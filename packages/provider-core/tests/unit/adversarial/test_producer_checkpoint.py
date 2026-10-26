"""Control codecs refuse full legacy records and foreign recovery identities."""

import json
import unittest
from dataclasses import replace

from provider_core.attempt_record import ProtectedEvidence, acknowledge_start, mark_execution_entered
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, EvidenceDigest, TerminalSelectedCheckpoint, decode_checkpoint, encode_checkpoint,
)
from provider_core.attempt_record import TerminalOperation
from tests.fakes.attempt_store import admission, record, scope


class ProducerCheckpointRefusalTests(unittest.TestCase):
    def test_full_prepared_evidence_cannot_be_encoded_as_control_metadata(self):
        legacy = mark_execution_entered(acknowledge_start(record(), admission()),
                                        ProtectedEvidence("fixture.prepared.v1", b"PRIVATE_COORDINATES"))
        with self.assertRaisesRegex(ValueError, "control checkpoint"):
            encode_checkpoint(legacy)

    def test_foreign_scope_and_undeclared_payload_field_are_not_silently_adopted(self):
        raw = encode_checkpoint(AcknowledgedCheckpoint(record(), admission()))
        with self.assertRaises(ValueError):
            decode_checkpoint(raw, scope=replace(scope(), generation_id="generation:other"))
        document = json.loads(raw)
        document["PRIVATE_PAYLOAD"] = "PRIVATE_COORDINATES"
        with self.assertRaises(ValueError) as refused:
            decode_checkpoint(json.dumps(document).encode(), scope=scope())
        self.assertNotIn("PRIVATE", str(refused.exception))

    def test_acknowledgement_and_selected_identity_must_match_their_actual_control_types(self):
        with self.assertRaises(ValueError):
            AcknowledgedCheckpoint(record(), replace(admission(), job_ref="job:other"))
        with self.assertRaises(ValueError):
            EvidenceDigest("fixture.prepared.v1", "PRIVATE_COORDINATES", 1)
        with self.assertRaises(ValueError):
            TerminalSelectedCheckpoint(AcknowledgedCheckpoint(record(), admission()), TerminalOperation.COMPLETE,
                                       "PRIVATE_BODY", 10)
