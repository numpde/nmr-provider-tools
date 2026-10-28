"""Live control projections identify decisions without retaining science."""

import hashlib
import json
import unittest

from provider_core.attempt_record import ProtectedEvidence, SuppressionReason, TerminalOperation
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint, EnteredCheckpoint, EvidenceDigest,
    SuppressedCheckpoint, TerminalSelectedCheckpoint, encode_checkpoint,
)
from tests.fakes.attempt_store import admission, record


class ProducerCheckpointTests(unittest.TestCase):
    def test_pending_start_and_acknowledgement_keep_original_request_without_live_handle(self):
        pending = record()
        self.assertEqual(json.loads(encode_checkpoint(pending))["kind"], "start_pending")
        acknowledged = AcknowledgedCheckpoint(pending, admission())
        self.assertEqual(json.loads(encode_checkpoint(acknowledged))["kind"], "acknowledged")
        self.assertIs(acknowledged.start, pending)
        self.assertEqual(acknowledged.start.body, pending.body)

    def test_execution_entry_keeps_only_the_prepared_identity_digest(self):
        evidence = ProtectedEvidence("fixture.prepared.v1", b"PRIVATE_COORDINATE_ARRAY")
        digest = EvidenceDigest.from_evidence(evidence)
        entered = EnteredCheckpoint(AcknowledgedCheckpoint(record(), admission()), digest)
        raw = encode_checkpoint(entered)
        self.assertNotIn(evidence.content, raw)
        self.assertNotIn(b"content", raw)
        self.assertEqual(digest.sha256, "sha256:" + hashlib.sha256(evidence.content).hexdigest())
        self.assertEqual(digest.byte_length, len(evidence.content))
        self.assertIs(entered.prepared, digest)

    def test_selected_command_survives_before_ack_without_claiming_relay_ownership(self):
        acknowledged = AcknowledgedCheckpoint(record(), admission())
        selected = TerminalSelectedCheckpoint(acknowledged, TerminalOperation.COMPLETE, "sha256:" + "b" * 64, 123)
        self.assertEqual(json.loads(encode_checkpoint(selected))["kind"], "terminal_selected")
        self.assertNotIsInstance(selected, DeliveryAcceptedCheckpoint)
        accepted = DeliveryAcceptedCheckpoint(selected)
        self.assertEqual(json.loads(encode_checkpoint(accepted))["kind"], "delivery_accepted")
        self.assertIs(accepted.selection, selected)

    def test_suppression_records_disposition_without_a_candidate_body(self):
        suppressed = SuppressedCheckpoint(AcknowledgedCheckpoint(record(), admission()), SuppressionReason.CALLER_CANCELLED)
        raw = encode_checkpoint(suppressed)
        self.assertEqual(json.loads(raw)["reason"], "caller_cancelled")
        self.assertNotIn(b"candidate", raw)
