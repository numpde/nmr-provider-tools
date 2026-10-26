"""Reject foreign observations without treating uncertain delivery as no change."""

import asyncio
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.attempts import ATTEMPT_REF, progress_response


class AttemptProgressFailureTests(unittest.IsolatedAsyncioTestCase):
    def api(self, transport):
        return ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )

    async def test_invalid_snapshot_never_dispatches(self):
        for phase, condition in (
            ("done", None),
            ([], None),
            ("preparing", "secret/url"),
            ("running", ""),
            ("running", "x" * 129),
            ("running", True),
        ):
            transport = Transport()
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                await self.api(transport).progress_attempt(ATTEMPT_REF, phase=phase, condition_code=condition)
            self.assertEqual(transport.calls, [])
        transport = Transport()
        with self.assertRaises(ValueError):
            await self.api(transport).progress_attempt(ATTEMPT_REF + "/../other", phase="running", condition_code=None)
        self.assertEqual(transport.calls, [])

    async def test_foreign_or_malformed_success_retains_uncertainty_and_evidence(self):
        for changes in (
            {"execution_attempt_ref": "execution_attempt:sha256:" + "b" * 64},
            {"phase": "running"},
            {"condition_code": "private_canary"},
            {"schema_id": "other"},
            {"extra": True},
            {"updated_at": "2026-02-30T00:00:00Z"},
            {"updated_at": "2026-10-18T00:00:00.000000Z"},
        ):
            response = reply(progress_response(**changes))
            transport = Transport(response)
            api = self.api(transport)
            with self.subTest(changes=changes), self.assertRaises(ApiFailure) as caught:
                await api.progress_attempt(ATTEMPT_REF, phase="preparing", condition_code=None)
            self.assertTrue(caught.exception.attempt_mutation_may_have_committed)
            self.assertIs(caught.exception.response, response)
            self.assertNotIn("private_canary", str(caught.exception))
            self.assertEqual(len(transport.calls), 1)

    async def test_reviewed_conflicts_refuse_this_update_while_interruption_stays_uncertain(self):
        for code in ("execution_attempt_progress_terminal", "execution_attempt_progress_regression"):
            problem = {
                "type": "urn:nmr-api:problem:operation-conflict",
                "title": "private-canary",
                "detail": "private-canary",
                "status": 409,
                "code": code,
                "instance": "urn:nmr-api:request:test",
                "request_id": "request:test",
            }
            transport = Transport(
                reply(json.dumps(problem).encode(), status=409, headers=response_headers(status=409, request_id=b"request:test"))
            )
            api = self.api(transport)
            with self.assertRaises(ApiFailure) as caught:
                await api.progress_attempt(ATTEMPT_REF, phase="preparing", condition_code=None)
            self.assertEqual(caught.exception.problem_code, code)
            self.assertFalse(caught.exception.attempt_mutation_may_have_committed)
            self.assertNotIn("private-canary", str(caught.exception))
            self.assertEqual(len(transport.calls), 1)
        for outcome in (ExchangeFailed("lost response"), asyncio.CancelledError()):
            transport = Transport(outcome)
            api = self.api(transport)
            with self.assertRaises(asyncio.CancelledError if isinstance(outcome, asyncio.CancelledError) else ApiFailure):
                await api.progress_attempt(ATTEMPT_REF, phase="running", condition_code=None)
            self.assertTrue(api.last_failure.attempt_mutation_may_have_committed)
            self.assertEqual(len(transport.calls), 1)
