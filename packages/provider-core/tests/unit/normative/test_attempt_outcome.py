"""Terminal retries retain bytes and acknowledge the exact published outcome."""

import json
import unittest
from hashlib import sha256

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.adapters.nmr.v1.attempt_request import complete_body, fail_body
from provider_core.adapters.nmr.v1.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response


class AttemptOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_replay_preserves_exact_request_bytes_and_acknowledged_content(self):
        for (method, body, response) in (
            (
                "complete_attempt",
                complete_body(
                    ATTEMPT_REF,
                    " result\n"
                    "雪 ",
                    b"result bytes",
                ),
                complete_response
            ),
            (
                "fail_attempt",
                fail_body(
                    ATTEMPT_REF,
                    "input_rejected",
                    " Explain\n"
                    "雪\x01 ",
                ),
                fail_response
            ),
        ):
            retained = json.dumps(json.loads(body), indent=2).encode() + b"\n"
            transport = Transport(reply(response()), reply(response(replayed=True)))

            api = ProviderAPI(
                SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )

            for replayed in (False, True):
                attempt_receipt = await getattr(api, method)(retained, execution_attempt_ref=ATTEMPT_REF)

                self.assertEqual(attempt_receipt.replayed, replayed)
                self.assertEqual(attempt_receipt.execution_attempt_ref, ATTEMPT_REF)
                self.assertEqual(attempt_receipt.committed_at.isoformat(), "2026-10-18T00:00:00+00:00")

            if method == "complete_attempt":
                self.assertEqual(
                    attempt_receipt.result_schema_id,
                    " result\n"
                    "雪 ",
                )

                self.assertEqual(attempt_receipt.result_byte_length, len(b"result bytes"))
                self.assertEqual(attempt_receipt.analysis_result_ref, "analysis_result:sha256:" + "b" * 64)
                self.assertNotIn("雪", repr(attempt_receipt))
            else:
                self.assertEqual(
                    attempt_receipt.failure_message,
                    " Explain\n"
                    "雪\x01 ",
                )

                self.assertNotIn("Explain", repr(attempt_receipt))

            (first, second) = (call[0] for call in transport.calls)

            self.assertEqual((first.content, second.content), (retained, retained))
            self.assertNotEqual(first.headers["Signature-Input"], second.headers["Signature-Input"])

            # silent-defaults: allow-contract -- The selected terminal method determines the expected complete or fail wire operation.
            operation = "complete" if method == "complete_attempt" else "fail"

            self.assertEqual(
                (first.method, first.url.raw_path),
                ("POST", b"/provider/v1/execution-attempts/" + operation.encode())
            )

    async def test_result_bytes_are_opaque_not_required_to_be_utf8_or_json(self):
        result_bytes = b"\xff\x00\x80\r\n"

        transport = Transport(
            reply(complete_response(result_fingerprint="sha256:" + sha256(result_bytes).hexdigest(), result_byte_length=len(result_bytes)))
        )

        api = ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )

        completion_receipt = await api.complete_attempt(
            complete_body(
                ATTEMPT_REF,
                " result\n"
                "雪 ",
                result_bytes,
            ),
            execution_attempt_ref=ATTEMPT_REF,
        )

        self.assertEqual(completion_receipt.result_byte_length, len(result_bytes))
