"""Corrupt or foreign-scope state cannot become permission to recover an Attempt."""

import unittest
from dataclasses import replace

from provider_core.attempt_record import RecoveryScope, StartPending, _encode_pending
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

    def test_encoder_refuses_record_above_the_live_control_limit(self):
        scope = replace(self.scope(), api_origin="https://" + "a" * 70000)
        record = StartPending(scope, "nmr_predict", facts(), retained_start())
        with self.assertRaisesRegex(ValueError, "65536"):
            _encode_pending(record)
