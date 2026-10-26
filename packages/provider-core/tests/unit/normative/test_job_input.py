"""Job input preserves exact original instructions before scientific admission."""

import unittest
from dataclasses import FrozenInstanceError

from provider_core.job_input import verify_job_input
from tests.fakes.job_input import facts, response


class JobInputTests(unittest.TestCase):
    def test_original_utf8_bytes_are_preserved_without_normalization(self):
        for payload in (b"x", b" \r\nprivate\x00instructions\t", "Café Cafe\u0301".encode(), b"x" * 65536):
            with self.subTest(length=len(payload)):
                verified = verify_job_input(response(payload), feed=facts(payload))
                self.assertEqual(verified.specification_bytes, payload)
                self.assertEqual(verified.job_ref, "job:example")
                self.assertEqual(verified.specification_text, payload.decode())
                self.assertNotIn("private", repr(verified))
                with self.assertRaises(FrozenInstanceError):
                    verified.specification_bytes = b"replacement"
