"""The final admitted candidate is retained before science may consume its handle."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from provider_core.analysis import PreparedAnalysis
from provider_core.attempt_record import MAX_EVIDENCE_BYTES, ProtectedEvidence
from provider_core.execution import AttemptExecutor
from provider_core.producer_checkpoint import (
    MAX_CHECKPOINT_BYTES, AcknowledgedCheckpoint, EnteredCheckpoint, EvidenceDigest,
    decode_checkpoint, encode_checkpoint,
)
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import observation, progress_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response

IDENTITY = ProtectedEvidence("fixture.prepared_input.v1", b"private candidate identity")


class PreparedIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_compact_entry_round_trip_keeps_digest_without_restoring_a_live_handle(self):
        entered = EnteredCheckpoint(AcknowledgedCheckpoint(record(), admission()), EvidenceDigest.from_evidence(IDENTITY))
        raw = encode_checkpoint(entered)
        document = json.loads(raw)
        self.assertEqual(document["kind"], "entered")
        self.assertEqual(set(document["prepared"]), {"schema_id", "sha256", "byte_length"})
        restored = decode_checkpoint(raw, scope=scope())
        self.assertEqual(restored, entered)
        self.assertEqual(encode_checkpoint(restored), raw)
        self.assertNotIsInstance(restored, PreparedAnalysis)
        self.assertNotIn(IDENTITY.content, raw)
        self.assertNotIn("private candidate", repr(restored))

    def test_largest_ram_identity_and_escaped_schema_fit_compact_entry_bound(self):
        identity = ProtectedEvidence("\U0001f600" * 1024, b"x" * MAX_EVIDENCE_BYTES)
        entered = EnteredCheckpoint(AcknowledgedCheckpoint(record(), admission()), EvidenceDigest.from_evidence(identity))
        raw = encode_checkpoint(entered)
        self.assertLessEqual(len(raw), MAX_CHECKPOINT_BYTES)
        self.assertEqual(entered, decode_checkpoint(raw, scope=scope()))
        self.assertNotIn(identity.content, raw)

    async def test_execute_observes_matching_identity_in_durable_entry_and_terminal(self):
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            reply(progress_response(phase="running")),
            reply(observation()),
            reply(observation()),
        )
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.handle = session.preparation = PreparedAnalysis(IDENTITY)
            try:
                terminal = await AttemptExecutor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                ).run(acknowledged)
                self.assertEqual(EvidenceDigest.from_evidence(IDENTITY), session.events[1][1].prepared)
                self.assertEqual(EvidenceDigest.from_evidence(IDENTITY), terminal.selection.attempt.prepared)
                self.assertEqual(terminal, decode_checkpoint(encode_checkpoint(terminal), scope=scope()))
                self.assertNotIn(IDENTITY.content, (Path(directory) / "checkpoints.sqlite3").read_bytes())
                claim.release()
            finally:
                await store.close()
