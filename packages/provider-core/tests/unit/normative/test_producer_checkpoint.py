"""Producer restart data identifies control decisions without keeping science."""

import hashlib
import unittest

from provider_core.attempt_record import ProtectedEvidence, SuppressionReason, TerminalOperation
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint, EnteredCheckpoint, EvidenceDigest,
    SuppressedCheckpoint, TerminalSelectedCheckpoint, decode_checkpoint, encode_checkpoint,
)
from tests.fakes.attempt_store import admission, record, scope


class ProducerCheckpointTests(unittest.TestCase):
    def test_pending_start_and_acknowledgement_keep_original_request_without_live_handle(self):
        pending = record()
        self.assertEqual(decode_checkpoint(encode_checkpoint(pending), scope=scope()), pending)
        acknowledged = AcknowledgedCheckpoint(pending, admission())
        restored = decode_checkpoint(encode_checkpoint(acknowledged), scope=scope())
        self.assertIs(type(restored), AcknowledgedCheckpoint)
        self.assertEqual(restored, acknowledged)
        self.assertEqual(restored.start.body, pending.body)

    def test_execution_entry_keeps_only_the_prepared_identity_digest(self):
        evidence = ProtectedEvidence("fixture.prepared.v1", b"PRIVATE_COORDINATE_ARRAY")
        digest = EvidenceDigest.from_evidence(evidence)
        entered = EnteredCheckpoint(AcknowledgedCheckpoint(record(), admission()), digest)
        raw = encode_checkpoint(entered)
        self.assertNotIn(evidence.content, raw)
        self.assertNotIn(b"content", raw)
        self.assertEqual(digest.sha256, "sha256:" + hashlib.sha256(evidence.content).hexdigest())
        self.assertEqual(digest.byte_length, len(evidence.content))
        self.assertEqual(decode_checkpoint(raw, scope=scope()), entered)

    def test_selected_command_survives_before_ack_without_claiming_relay_ownership(self):
        acknowledged = AcknowledgedCheckpoint(record(), admission())
        selected = TerminalSelectedCheckpoint(acknowledged, TerminalOperation.COMPLETE, "sha256:" + "b" * 64, 123)
        restored = decode_checkpoint(encode_checkpoint(selected), scope=scope())
        self.assertIs(type(restored), TerminalSelectedCheckpoint)
        self.assertNotIsInstance(restored, DeliveryAcceptedCheckpoint)
        self.assertEqual(restored, selected)
        accepted = DeliveryAcceptedCheckpoint(selected)
        self.assertEqual(decode_checkpoint(encode_checkpoint(accepted), scope=scope()), accepted)

    def test_suppression_records_disposition_without_a_candidate_body(self):
        suppressed = SuppressedCheckpoint(AcknowledgedCheckpoint(record(), admission()), SuppressionReason.CALLER_CANCELLED)
        raw = encode_checkpoint(suppressed)
        self.assertEqual(decode_checkpoint(raw, scope=scope()), suppressed)
        self.assertNotIn(b"candidate", raw)
