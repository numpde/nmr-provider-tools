"""Job input preserves exact original instructions before scientific admission."""

import unittest
import os
from dataclasses import FrozenInstanceError

from provider_core.validation.upstream.nmr.v1.job_input import verify_job_input
from tests.fakes.job_input import facts, response


class JobInputTests(unittest.TestCase):
    def test_original_utf8_bytes_are_preserved_without_normalization(self):
        for payload in (
            b"x",
            (
                b" \r\n"
                b"private\x00instructions\t"
            ),
            "Café Cafe\u0301".encode(),
            b"x" * 65536
        ):
            with self.subTest(length=len(payload)):
                verified = verify_job_input(response(payload), feed=facts(payload))

                self.assertEqual(verified.specification_bytes, payload)
                self.assertEqual(verified.job_ref, "job:example")
                self.assertEqual(verified.specification_text, payload.decode())

                with self.assertRaises(FrozenInstanceError):
                    verified.specification_bytes = b"replacement"

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_verified_input_representation_omits_instructions(self):
        payload = b"private\x00instructions"
        verified = verify_job_input(response(payload), feed=facts(payload))

        self.assertNotIn("private", repr(verified))
