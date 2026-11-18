"""Verify the file handed to a consumer and retain its original handle."""

import pathlib
from hashlib import sha256
from io import BytesIO
import unittest
from unittest.mock import patch

from provider_core.verified_file import open_verified_file


class VerifiedFileTests(unittest.TestCase):
    def test_verified_handle_is_rewound_for_the_consumer(self):
        contents = b"model checkpoint"
        opened_file = BytesIO(contents)
        expected_sha256 = sha256(contents).hexdigest()

        with patch.object(target=pathlib.Path, attribute="open", return_value=opened_file):
            with open_verified_file(source_file_path=pathlib.Path("model.bin"), expected_sha256=expected_sha256) as verified_file:
                self.assertIs(expr1=opened_file, expr2=verified_file)
                self.assertEqual(first=contents, second=verified_file.read())

        self.assertTrue(expr=opened_file.closed)

    def test_mismatch_closes_the_file_and_reports_both_digests(self):
        contents = b"model checkpoint"
        opened_file = BytesIO(contents)
        expected_sha256 = "0" * 64

        with patch.object(target=pathlib.Path, attribute="open", return_value=opened_file):
            with self.assertRaises(expected_exception=ValueError) as failure:
                with open_verified_file(source_file_path=pathlib.Path("model.bin"), expected_sha256=expected_sha256):
                    self.fail("A mismatched file must not be handed to the consumer")

        self.assertTrue(expr=opened_file.closed)
        self.assertIn(member=sha256(contents).hexdigest(), container=str(failure.exception))
        self.assertIn(member=expected_sha256, container=str(failure.exception))

    def test_consumer_failure_closes_the_file_and_propagates(self):
        contents = b"model checkpoint"
        opened_file = BytesIO(contents)
        failure = OSError("Model loading failed")

        with patch.object(target=pathlib.Path, attribute="open", return_value=opened_file):
            with self.assertRaises(expected_exception=OSError) as raised:
                with open_verified_file(source_file_path=pathlib.Path("model.bin"), expected_sha256=sha256(contents).hexdigest()):
                    raise failure

        self.assertIs(expr1=failure, expr2=raised.exception)
        self.assertTrue(expr=opened_file.closed)
