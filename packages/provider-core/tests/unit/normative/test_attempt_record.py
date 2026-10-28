"""A pending start retains the original request under an explicit recovery scope."""

import unittest

from provider_core.attempt_record import RecoveryScope, StartPending, _encode_pending
from tests.fakes.attempts import retained_start
from tests.fakes.job_input import facts


class AttemptRecordTests(unittest.TestCase):
    def test_pending_start_projection_preserves_exact_request_and_scope(self):
        scope = RecoveryScope("https://api.example", "provider:example", "lane:carbon", "generation:one")
        record = StartPending(scope, "nmr_predict", facts(), retained_start())
        raw = _encode_pending(record)
        self.assertEqual(record.body, retained_start())
        self.assertEqual(record.scope, scope)
        self.assertTrue(raw)
        self.assertNotIn("retained-start:01", repr(record))

    def test_maximum_retained_start_and_scope_text_remain_encodable(self):
        scope = RecoveryScope("https://api.example", "provider:example", "雪" * 1024, "\U0001f600" * 1024)
        original = retained_start()
        body = original + b" " * (4096 - len(original))
        record = StartPending(scope, "nmr_predict", facts(), body)
        self.assertLessEqual(len(_encode_pending(record)), 65536)
