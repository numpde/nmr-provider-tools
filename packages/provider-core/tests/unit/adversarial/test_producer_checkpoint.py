"""Control codecs refuse full legacy records and foreign recovery identities."""

import unittest
from dataclasses import replace

from provider_core.attempt_record import ProtectedEvidence, acknowledge_start, mark_execution_entered
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, EvidenceDigest, TerminalSelectedCheckpoint, encode_checkpoint,
)
from provider_core.attempt_record import TerminalOperation
from tests.fakes.attempt_store import admission, record


class ProducerCheckpointRefusalTests(unittest.TestCase):
    def test_full_prepared_evidence_cannot_be_encoded_as_control_metadata(self):
        legacy = mark_execution_entered(acknowledge_start(record(), admission()),
                                        ProtectedEvidence("fixture.prepared.v1", b"PRIVATE_COORDINATES"))
        with self.assertRaisesRegex(ValueError, "control checkpoint"):
            encode_checkpoint(legacy)


    def test_acknowledgement_and_selected_identity_must_match_their_actual_control_types(self):
        with self.assertRaises(ValueError):
            AcknowledgedCheckpoint(record(), replace(admission(), job_ref="job:other"))
        with self.assertRaises(ValueError):
            EvidenceDigest("fixture.prepared.v1", "PRIVATE_COORDINATES", 1)
        with self.assertRaises(ValueError):
            TerminalSelectedCheckpoint(AcknowledgedCheckpoint(record(), admission()), TerminalOperation.COMPLETE,
                                       "PRIVATE_BODY", 10)
