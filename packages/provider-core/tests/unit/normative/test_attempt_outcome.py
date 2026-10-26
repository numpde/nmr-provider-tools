"""Terminal retries retain bytes and acknowledge the exact published outcome."""

import json
import unittest
from hashlib import sha256

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.attempt_request import complete_body, fail_body
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response


class AttemptOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_replay_preserves_exact_request_bytes_and_acknowledged_content(self):
        for method, body, response in (
            ("complete_attempt", complete_body(ATTEMPT_REF, " result\n雪 ", b"result bytes"), complete_response),
            ("fail_attempt", fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 "), fail_response),
        ):
            retained = json.dumps(json.loads(body), indent=2).encode() + b"\n"
            transport = Transport(reply(response()), reply(response(replayed=True)))
            api = ProviderAPI(
                SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            for replayed in (False, True):
                result = await getattr(api, method)(retained, execution_attempt_ref=ATTEMPT_REF)
                self.assertEqual(result.replayed, replayed)
                self.assertEqual(result.execution_attempt_ref, ATTEMPT_REF)
                self.assertEqual(result.committed_at.isoformat(), "2026-10-18T00:00:00+00:00")
            if method == "complete_attempt":
                self.assertEqual(result.result_schema_id, " result\n雪 ")
                self.assertEqual(result.result_byte_length, len(b"result bytes"))
                self.assertEqual(result.analysis_result_ref, "analysis_result:sha256:" + "b" * 64)
                self.assertNotIn("雪", repr(result))
            else:
                self.assertEqual(result.failure_message, " Explain\n雪\x01 ")
                self.assertNotIn("Explain", repr(result))
            first, second = (call[0] for call in transport.calls)
            self.assertEqual((first.content, second.content), (retained, retained))
            self.assertNotEqual(first.headers["Signature-Input"], second.headers["Signature-Input"])
            # silent-defaults: allow-contract -- The selected terminal method determines the expected complete or fail wire operation.
            operation = "complete" if method == "complete_attempt" else "fail"
            self.assertEqual(
                (first.method, first.url.raw_path), ("POST", b"/provider/v1/execution-attempts/" + operation.encode())
            )

    def test_builders_preserve_maximum_payloads_and_scalar_text(self):
        result = b"x" * 786432
        body = complete_body(ATTEMPT_REF, "雪" * 1024, result)
        self.assertLessEqual(len(body), 2097152)
        self.assertEqual(len(json.loads(body)["canonical_result_base64"]), 1048576)
        message = "\U0001f600" * 1024
        body = fail_body(ATTEMPT_REF, "x" * 128, message)
        self.assertLessEqual(len(body), 16384)
        self.assertEqual(json.loads(body)["failure_message"], message)

    async def test_result_bytes_are_opaque_not_required_to_be_utf8_or_json(self):
        data = b"\xff\x00\x80\r\n"
        transport = Transport(
            reply(complete_response(result_fingerprint="sha256:" + sha256(data).hexdigest(), result_byte_length=len(data)))
        )
        api = ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )
        result = await api.complete_attempt(complete_body(ATTEMPT_REF, " result\n雪 ", data), execution_attempt_ref=ATTEMPT_REF)
        self.assertEqual(result.result_byte_length, len(data))
