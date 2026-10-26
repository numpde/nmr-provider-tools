"""Corrupt or foreign-scope state cannot become permission to recover an Attempt."""

import json
import unittest
from dataclasses import replace

from provider_core.attempt_record import RecoveryScope, StartPending, _decode_pending, _encode_pending
from tests.fakes.attempts import retained_start
from tests.fakes.job_input import facts


class AttemptRecordFailureTests(unittest.TestCase):
    def scope(self):
        return RecoveryScope("https://api.example", "provider:example", "lane:carbon", "generation:one")

    def record(self):
        return StartPending(self.scope(), "nmr_predict", facts(), retained_start())

    def test_inconsistent_or_mutable_start_is_rejected(self):
        for body in (retained_start(job_ref="job:foreign"), bytearray(retained_start())):
            with self.assertRaises(ValueError):
                StartPending(self.scope(), "nmr_predict", facts(), body)
        with self.assertRaises(ValueError):
            StartPending(self.scope(), "bad/key", facts(), retained_start())

    def test_foreign_api_provider_lane_and_generation_block_restore(self):
        raw = _encode_pending(self.record())
        for changes in (
            {"api_origin": "https://other.example"},
            {"provider_ref": "provider:other"},
            {"consumer_id": "lane:hydrogen"},
            {"generation_id": "generation:two"},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "scope"):
                _decode_pending(raw, scope=replace(self.scope(), **changes))

    def test_unsupported_and_corrupt_records_fail_without_private_text(self):
        good = json.loads(_encode_pending(self.record()))
        for document in (
            good | {"schema_id": "future"},
            good | {"extra": "private-canary"},
            good | {"state": "private-canary"},
            good | {"scope": {}},
            good | {"input": {}},
            good | {"start_body": "eB=="},
            {},
            [],
        ):
            with self.subTest(document_type=type(document).__name__), self.assertRaises(ValueError) as caught:
                _decode_pending(json.dumps(document).encode(), scope=self.scope())
            self.assertNotIn("private-canary", str(caught.exception))
        for raw in (b"\xff", b"{}" + b" " * 65536, b'{"state":"a","state":"b"}', b'[{"x":NaN}]'):
            with self.assertRaises(ValueError):
                _decode_pending(raw, scope=self.scope())

    def test_invalid_scope_is_not_a_recovery_owner(self):
        for changes in (
            {"api_origin": "http://api.example"},
            {"api_origin": "https://api.example/path"},
            {"provider_ref": "bad"},
            {"consumer_id": ""},
            {"generation_id": "\ud800"},
            {"consumer_id": "x" * 1025},
        ):
            with self.assertRaises(ValueError):
                replace(self.scope(), **changes)

    def test_encoder_refuses_record_larger_than_decoder_can_restore(self):
        scope = replace(self.scope(), api_origin="https://" + "a" * 70000)
        record = StartPending(scope, "nmr_predict", facts(), retained_start())
        with self.assertRaisesRegex(ValueError, "65536"):
            _encode_pending(record)
