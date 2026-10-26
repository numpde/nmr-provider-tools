"""Lost or rejected admission must not silently become a new logical start."""

import asyncio
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.attempts import retained_start, start_response


class AttemptStartFailureTests(unittest.IsolatedAsyncioTestCase):
    def api(self, transport):
        return ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )

    async def start(self, api, body=None, **changes):
        facts = {"job_ref": "job:example", "analysis_kind_ref": "nmr_predict", "provider_ref": "provider:example"} | changes
        return await api.start_attempt(retained_start() if body is None else body, **facts)

    async def test_invalid_retained_request_or_expected_identity_never_sends(self):
        for body in (
            retained_start(job_ref="job:other"),
            retained_start(provider_attempt_key=""),
            retained_start(provider_attempt_key="x" * 129),
            retained_start(extra=True),
            retained_start(schema_id="wrong"),
            b"{}",
            b"\xff",
            b" " * 4097,
            bytearray(retained_start()),
            retained_start().replace(b"{", b'{"job_ref":"job:example",', 1),
        ):
            transport = Transport()
            with self.subTest(body_type=type(body)), self.assertRaises(ValueError):
                await self.start(self.api(transport), body)
            self.assertEqual(transport.calls, [])
        for facts in ({"analysis_kind_ref": "bad?key"}, {"provider_ref": "bad"}, {"job_ref": "job:bad/"}):
            transport = Transport()
            with self.assertRaises(ValueError):
                await self.start(self.api(transport), **facts)
            self.assertEqual(transport.calls, [])

    async def test_malformed_success_keeps_admission_uncertain_and_private(self):
        for changes in (
            {"job_ref": "job:other"},
            {"analysis_kind_ref": "other"},
            {"provider_ref": "provider:other"},
            {"execution_attempt_ref": "bad"},
            {"replayed": 1},
            {"state": []},
            {"extra": "private-canary"},
            {"schema_id": "bad"},
            {"started_at": "2026-02-30T00:00:00Z"},
            {"started_at": "2026-10-18T00:00:00.000000Z"},
        ):
            response = reply(start_response(**changes))
            transport = Transport(response)
            api = self.api(transport)
            with self.subTest(changes=changes), self.assertRaises(ApiFailure) as caught:
                await self.start(api)
            self.assertTrue(caught.exception.attempt_mutation_may_have_committed)
            self.assertIs(caught.exception.response, response)
            self.assertIs(api.last_failure, caught.exception)
            self.assertNotIn("private-canary", str(caught.exception))
            self.assertEqual(len(transport.calls), 1)

    async def test_transport_cancel_and_bad_envelope_keep_uncertainty(self):
        for outcome in (
            ExchangeFailed("transport failed"),
            asyncio.CancelledError(),
            reply(b"private-canary", status=503),
            reply(start_response(), headers=()),
        ):
            transport = Transport(outcome)
            api = self.api(transport)
            with self.assertRaises(asyncio.CancelledError if isinstance(outcome, asyncio.CancelledError) else ApiFailure):
                await self.start(api)
            self.assertTrue(api.last_failure.attempt_mutation_may_have_committed)
            self.assertFalse(api.last_failure.issuance_may_have_committed)
            self.assertEqual(len(transport.calls), 1)

    async def test_current_send_refusal_does_not_erase_prior_uncertainty(self):
        for status, kind, code, uncertain in (
            (401, "authentication-failed", "signature_expired", False),
            (409, "operation-conflict", "provider_attempt_key_conflict", False),
            (409, "operation-conflict", "job_provider_attempt_limit_reached", False),
            (409, "operation-conflict", "job_attempt_limit_reached", False),
            (409, "operation-conflict", "project_purge_in_progress", True),
            (403, "authorization-denied", "authorization_denied", True),
            (404, "not-found", "resource_not_found", True),
            (503, "service-unavailable", "service_unavailable", True),
        ):
            document = {
                "type": "urn:nmr-api:problem:" + kind,
                "title": "private-canary",
                "status": status,
                "instance": "urn:nmr-api:request:test",
                "request_id": "request:test",
                "code": code,
                "detail": "private-canary",
            }
            transport = Transport(
                ExchangeFailed("response lost"),
                reply(json.dumps(document).encode(), status=status, headers=response_headers(status=status, request_id=b"request:test")),
            )
            api = self.api(transport)
            with self.assertRaises(ApiFailure) as earlier:
                await self.start(api)
            with self.assertRaises(ApiFailure) as current:
                await self.start(api)
            self.assertTrue(earlier.exception.attempt_mutation_may_have_committed)
            self.assertEqual(current.exception.attempt_mutation_may_have_committed, uncertain)
            self.assertNotIn("private-canary", str(current.exception))
