"""Rejected Attempt input must not produce a terminal publication body."""

import unittest

from provider_core.adapters.nmr.v1.attempt_request import complete_body, fail_body
from tests.fakes.attempts import ATTEMPT_REF


class AttemptRequestEncodingTests(unittest.TestCase):
    def test_invalid_builder_inputs_do_not_create_publication(self):
        for (data, error_type) in ((b"", ValueError), (b"x" * 786433, ValueError), (bytearray(b"x"), TypeError)):
            with self.assertRaises(error_type):
                complete_body(ATTEMPT_REF, "schema", data)

        for message in ("", "\x00", "\udfff", "x" * 1025):
            with self.assertRaises(ValueError):
                fail_body(ATTEMPT_REF, "input_rejected", message)
