"""Readback cannot substitute an Attempt, Job, or unsupported lifecycle state."""

import asyncio
import unittest
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.api_errors import ApiFailure
from provider_core.adapters.nmr.v1.signing import SigningIdentity
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
        for (attempt, job, error_type) in (
            (ATTEMPT_REF + "\n", "job:example", ValueError),
            ("../private", "job:example", ValueError),
            (None, "job:example", TypeError),
            (ATTEMPT_REF, "job:example?private", ValueError),
        ):
            transport = Transport()

            with self.assertRaises(error_type):
                await self.api(transport).read_attempt(attempt, job_ref=job)

            self.assertEqual(transport.calls, [])

    async def _malformed_observation_failures(self):
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

                yield caught.exception, response, api, transport

    async def test_malformed_or_foreign_observations_retain_evidence(self):
        async for (failure, response, api, transport) in self._malformed_observation_failures():
            self.assertIs(failure.response, response)
            self.assertIs(api.last_failure, failure)
            self.assertEqual(len(transport.calls), 1)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_malformed_observation_diagnostics_omit_response_content(self):
        async for (failure, _response, _api, _transport) in self._malformed_observation_failures():
            self.assertNotIn("private-canary", str(failure))

    async def _failed_reads(self):
        failure = ExchangeFailed("read failed", primary=OSError("private-canary"))

        for outcome in (reply(b"private-canary", status=503), reply(observation(), headers=()), failure):
            transport = Transport(outcome)
            api = self.api(transport)

            with self.assertRaises(ApiFailure) as caught:
                await api.read_attempt(ATTEMPT_REF, job_ref="job:example")

            yield caught.exception, api, transport

    async def test_failed_reads_preserve_transport_and_http_evidence_without_retry(self):
        async for (failure, api, transport) in self._failed_reads():
            self.assertIs(api.last_failure, failure)
            self.assertEqual(len(transport.calls), 1)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_failed_read_diagnostics_omit_response_and_transport_content(self):
        async for (failure, _api, _transport) in self._failed_reads():
            self.assertNotIn("private-canary", str(failure))

    async def test_cancellation_remains_cancellation_with_operation_context(self):
        transport = Transport(asyncio.CancelledError())
        api = self.api(transport)

        with self.assertRaises(asyncio.CancelledError):
            await api.read_attempt(ATTEMPT_REF, job_ref="job:example")

        self.assertEqual(api.last_failure.operation, "read Execution Attempt")
        self.assertEqual(len(transport.calls), 1)
