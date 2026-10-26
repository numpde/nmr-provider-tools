"""Untrusted API facts cannot substitute input or expose private response text."""

import base64
import json
import traceback
import unittest

from provider_core.job_input import FeedInputFacts, JobInputRejected, verify_job_input
from tests.fakes.job_input import facts, response


class UntrustedJobInputTests(unittest.TestCase):
    def rejected(self, raw, feed=None):
        try:
            verify_job_input(raw, feed=facts() if feed is None else feed)
        except JobInputRejected as error:
            self.assertNotIn("PRIVATE_SENTINEL", traceback.format_exc())
            self.assertIn("Job input", str(error))
        else:
            self.fail("Untrusted input was accepted")

    def test_every_identity_substitution_is_rejected(self):
        changes = (
            {"job_ref": "job:other"},
            {"input_schema_id": "PRIVATE_SENTINEL"},
            {"input_fingerprint": "sha256:" + "0" * 64},
            {"input_byte_length": 1},
            {"schema_id": "PRIVATE_SENTINEL"},
        )
        for change in changes:
            with self.subTest(field=next(iter(change))):
                self.rejected(response(**change))

    def test_malformed_private_documents_are_safe_rejections(self):
        for raw in (
            b"PRIVATE_SENTINEL",
            b"[]",
            b"null",
            b"\xff",
            b" " * 131073,
            b'{"PRIVATE_SENTINEL":1,"PRIVATE_SENTINEL":2}',
            b'{"job_ref":"a","job_\\u0072ef":"b"}',
            b"[" * 2000 + b"]" * 2000,
            b'{"PRIVATE_SENTINEL":' + b"1" * 5000 + b"}",
            response(PRIVATE_SENTINEL="secret"),
        ):
            with self.subTest(size=len(raw)):
                self.rejected(raw)

    def test_invalid_base64_and_noncanonical_pad_bits_are_rejected(self):
        for encoded in ("PRIVATE_SENTINEL", " eA==", "eA==\n", "eA", "eB==", "é"):
            self.rejected(response(b"x", canonical_input_base64=encoded), facts(b"x"))

    def test_decoded_bytes_must_match_length_digest_and_utf8(self):
        self.rejected(response(canonical_input_base64="eA=="))
        self.rejected(response(canonical_input_base64=base64.b64encode(b"X" * len(b"private instructions")).decode()))
        self.rejected(response(b"\xff"), facts(b"\xff"))

    def test_lengths_are_bounded_integers_not_coerced(self):
        for length in (True, 0, -1, 1.0, 65537, "PRIVATE_SENTINEL"):
            with self.subTest(length=length):
                self.rejected(response(input_byte_length=length))

    def test_feed_facts_are_validated_before_use(self):
        for change in (
            {"job_ref": "job:../PRIVATE_SENTINEL"},
            {"input_schema_id": "other"},
            {"input_fingerprint": "sha256:" + "A" * 64},
            {"input_byte_length": True},
        ):
            fields = {
                "job_ref": "job:example",
                "input_schema_id": "nmr.job.specification.text.v1",
                "input_fingerprint": "sha256:" + "0" * 64,
                "input_byte_length": 1,
            }
            fields.update(change)
            with self.assertRaises(JobInputRejected):
                FeedInputFacts(**fields)

    def test_missing_field_and_duplicate_equal_field_are_rejected(self):
        document = json.loads(response())
        del document["job_ref"]
        self.rejected(json.dumps(document).encode())
        self.rejected(response()[:-1] + b',"job_ref":"job:example"}')

    def test_mutable_response_is_not_admitted(self):
        self.rejected(bytearray(response()))
