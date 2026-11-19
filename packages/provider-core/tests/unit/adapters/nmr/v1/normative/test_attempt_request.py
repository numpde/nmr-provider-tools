"""Attempt encoding preserves explicit identities and complete terminal payloads."""

import json
import unittest

from provider_core.adapters.nmr.v1.attempt_request import complete_body, fail_body, start_body
from tests.fakes.attempts import ATTEMPT_REF


class AttemptRequestEncodingTests(unittest.TestCase):
    def test_builder_preserves_explicit_key_without_inventing_identity(self):
        body = start_body("job:example", "persisted:key-1")

        self.assertIs(type(body), bytes)

        self.assertEqual(
            json.loads(body),
            {
                "schema_id": "nmr.provider.execution_attempt_start_request.v1",
                "job_ref": "job:example",
                "provider_attempt_key": "persisted:key-1",
            },
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
