"""Readback cannot substitute an Attempt, Job, or unsupported lifecycle state."""

import asyncio
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import ATTEMPT_REF, observation


class AttemptReadRejectionTests(unittest.IsolatedAsyncioTestCase):
    def api(self, transport):
        return ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )

    async def test_invalid_request_identity_sends_nothing(self):
        for attempt, job in (
            (ATTEMPT_REF + "\n", "job:example"),
            ("../private", "job:example"),
            (None, "job:example"),
            (ATTEMPT_REF, "job:example?private"),
        ):
            transport = Transport()
            with self.assertRaises(ValueError):
                await self.api(transport).read_attempt(attempt, job_ref=job)
            self.assertEqual(transport.calls, [])

    async def test_malformed_or_foreign_observations_retain_private_evidence(self):
        bodies = [
            observation(execution_attempt_ref="execution_attempt:sha256:" + "b" * 64),
            observation(job_ref="job:foreign"),
            observation(schema_id="private-canary"),
            observation(state="private-canary"),
            observation(job_state="private-canary"),
            observation(state=[]),
            observation(job_state=False),
            observation(job_state=["open"]),
            observation(job_state={"state": "open"}),
            observation(extra="private-canary"),
            b"{}",
            b"[]",
            b"{" + b'"state":"private-canary",' + observation()[1:],
            b"\xffprivate-canary",
            b'{"private-canary":NaN}',
            b" " * 65537,
            b"[" * 2000 + b"]" * 2000,
        ]
        for body in bodies:
            with self.subTest(body_prefix=body[:30]):
                response = reply(body)
                transport = Transport(response)
                api = self.api(transport)
                with self.assertRaises(ApiFailure) as caught:
                    await api.read_attempt(ATTEMPT_REF, job_ref="job:example")
                self.assertNotIn("private-canary", str(caught.exception))
                self.assertIs(caught.exception.response, response)
                self.assertIs(api.last_failure, caught.exception)
                self.assertEqual(len(transport.calls), 1)

    async def test_failed_reads_preserve_transport_and_http_evidence_without_retry(self):
        failure = ExchangeFailed("read failed", primary=OSError("private-canary"))
        for outcome in (reply(b"private-canary", status=503), reply(observation(), headers=()), failure):
            transport = Transport(outcome)
            api = self.api(transport)
            with self.assertRaises(ApiFailure) as caught:
                await api.read_attempt(ATTEMPT_REF, job_ref="job:example")
            self.assertNotIn("private-canary", str(caught.exception))
            self.assertIs(api.last_failure, caught.exception)
            self.assertEqual(len(transport.calls), 1)

    async def test_cancellation_remains_cancellation_with_operation_context(self):
        transport = Transport(asyncio.CancelledError())
        api = self.api(transport)
        with self.assertRaises(asyncio.CancelledError):
            await api.read_attempt(ATTEMPT_REF, job_ref="job:example")
        self.assertEqual(api.last_failure.operation, "read Execution Attempt")
        self.assertEqual(len(transport.calls), 1)
