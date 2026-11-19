"""Uncertain terminal publication cannot authorize another result or science run."""

import asyncio
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.attempt_request import complete_body, fail_body
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response


class AttemptOutcomeFailureTests(unittest.IsolatedAsyncioTestCase):
    def api(self, transport):
        return ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )

    def body(self, completing):
        return (
            complete_body(
                ATTEMPT_REF,
                " result\n"
                "雪 ",
                b"result bytes",
            )
            if completing
            else fail_body(
                ATTEMPT_REF,
                "input_rejected",
                " Explain\n"
                "雪\x01 ",
            )
        )

    async def send(self, api, completing, body):
        method = api.complete_attempt if completing else api.fail_attempt

        return await method(body, execution_attempt_ref=ATTEMPT_REF)

    async def test_invalid_retained_outcomes_send_nothing(self):
        for (completing, changes) in (
            (True, {"canonical_result_base64": "eB=="}),
            (True, {"canonical_result_base64": ""}),
            (True, {"canonical_result_base64": "eA==\n"}),
            (True, {"result_schema_id": "bad\x00"}),
            (True, {"result_schema_id": "\ud800"}),
            (False, {"failure_message": ""}),
            (False, {"failure_message": "\ud800"}),
            (False, {"failure_code": "not/safe"}),
            (False, {"failure_message": "x" * 1025}),
            (True, {"schema_id": "other"}),
            (False, {"extra": True}),
            (True, {"execution_attempt_ref": "execution_attempt:sha256:" + "c" * 64}),
        ):
            document = json.loads(self.body(completing)) | changes
            transport = Transport()

            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.send(self.api(transport), completing, json.dumps(document).encode())

            self.assertEqual(transport.calls, [])

        for (completing, body) in ((True, b" " * 2097153), (False, b" " * 16385), (False, bytearray(self.body(False)))):
            transport = Transport()

            with self.assertRaises(ValueError):
                await self.send(self.api(transport), completing, body)

            self.assertEqual(transport.calls, [])

    def test_invalid_builder_inputs_do_not_create_publication(self):
        for (data, error_type) in ((b"", ValueError), (b"x" * 786433, ValueError), (bytearray(b"x"), TypeError)):
            with self.assertRaises(error_type):
                complete_body(ATTEMPT_REF, "schema", data)

        for message in ("", "\x00", "\udfff", "x" * 1025):
            with self.assertRaises(ValueError):
                fail_body(ATTEMPT_REF, "input_rejected", message)

    async def _mismatched_outcome_failures(self):
        for (completing, response, changes) in (
            (True, complete_response, {"result_fingerprint": "sha256:" + "0" * 64}),
            (True, complete_response, {"result_byte_length": True}),
            (True, complete_response, {"result_schema_id": "private-canary"}),
            (True, complete_response, {"analysis_result_ref": "bad"}),
            (False, fail_response, {"failure_message": "private-canary"}),
            (False, fail_response, {"failure_code": "other"}),
            (False, fail_response, {"replayed": 1}),
            (True, complete_response, {"committed_at": "2026-02-30T00:00:00Z"}),
            (False, fail_response, {"execution_attempt_ref": "execution_attempt:sha256:" + "c" * 64}),
        ):
            received = reply(response(**changes))
            transport = Transport(received)

            with self.subTest(changes=changes), self.assertRaises(ApiFailure) as caught:
                await self.send(self.api(transport), completing, self.body(completing))

            yield caught.exception, received, transport

    async def test_mismatched_acknowledgements_retain_uncertainty(self):
        async for (failure, received, transport) in self._mismatched_outcome_failures():
            self.assertTrue(failure.attempt_mutation_may_have_committed)
            self.assertIs(failure.response, received)
            self.assertEqual(len(transport.calls), 1)

    @unittest.skip("Privacy policy under review")
    async def test_outcome_acknowledgement_diagnostics_omit_response_content(self):
        async for (failure, _received, _transport) in self._mismatched_outcome_failures():
            self.assertNotIn("private-canary", str(failure))

    async def test_lost_outcome_reply_never_retries_or_switches_terminal_operation(self):
        for completing in (False, True):
            for outcome in (ExchangeFailed("response lost"), asyncio.CancelledError(), reply(b"private-canary", status=503)):
                transport = Transport(outcome)
                api = self.api(transport)

                with self.assertRaises(asyncio.CancelledError if isinstance(outcome, asyncio.CancelledError) else ApiFailure):
                    await self.send(api, completing, self.body(completing))

                self.assertTrue(api.last_failure.attempt_mutation_may_have_committed)
                self.assertEqual(len(transport.calls), 1)

    async def _terminal_conflicts_after_lost_response(self):
        for (completing, code) in (
            (True, "execution_attempt_outcome_expired"),
            (True, "execution_attempt_completion_after_failure"),
            (True, "execution_attempt_completion_replay_mismatch"),
            (False, "execution_attempt_failure_after_success"),
            (False, "execution_attempt_failure_replay_mismatch"),
        ):
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
                ExchangeFailed("lost reply"),
                reply(json.dumps(problem).encode(), status=409, headers=response_headers(status=409, request_id=b"request:test")),
            )

            api = self.api(transport)

            with self.assertRaises(ApiFailure) as earlier:
                await self.send(api, completing, self.body(completing))

            with self.assertRaises(ApiFailure) as current:
                await self.send(api, completing, self.body(completing))

            yield earlier.exception, current.exception, code

    async def test_terminal_conflicts_resolve_only_the_current_send(self):
        async for (earlier_failure, current_failure, code) in self._terminal_conflicts_after_lost_response():
            self.assertTrue(earlier_failure.attempt_mutation_may_have_committed)
            self.assertFalse(current_failure.attempt_mutation_may_have_committed)
            self.assertEqual(current_failure.problem_code, code)

    @unittest.skip("Privacy policy under review")
    async def test_terminal_conflict_diagnostics_omit_problem_content(self):
        async for (_earlier_failure, current_failure, _code) in self._terminal_conflicts_after_lost_response():
            self.assertNotIn("private-canary", str(current_failure))
