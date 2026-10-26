"""Server admission and local execution entry remain independent durable facts."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from provider_core.attempt_record import (
    MAX_PENDING_RECORD_BYTES,
    LocalExecutionPhase,
    acknowledge_start,
    _encode_pending,
    mark_execution_entered,
)
from provider_core.checkpoint_store import CheckpointLimits, CheckpointStore
from provider_core.producer_checkpoint import (
    MAX_CHECKPOINT_BYTES, AcknowledgedCheckpoint, EnteredCheckpoint, EvidenceDigest, decode_checkpoint, encode_checkpoint,
)
from tests.fakes.attempt_store import PREPARED_IDENTITY, admission, record, scope


class AttemptTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_start_fits_compact_acknowledgement_and_entry(self):
        pending = record()
        growth = MAX_PENDING_RECORD_BYTES - len(_encode_pending(pending))
        large_scope = replace(scope(), api_origin=scope().api_origin + "a" * growth)
        pending = replace(pending, scope=large_scope)
        self.assertEqual(len(_encode_pending(pending)), MAX_PENDING_RECORD_BYTES)
        acknowledged = AcknowledgedCheckpoint(pending, admission())
        entered = EnteredCheckpoint(acknowledged, EvidenceDigest.from_evidence(PREPARED_IDENTITY))
        for checkpoint in (pending, acknowledged, entered):
            with self.subTest(state=type(checkpoint).__name__):
                raw = encode_checkpoint(checkpoint)
                self.assertLessEqual(len(raw), MAX_CHECKPOINT_BYTES)
                self.assertEqual(decode_checkpoint(raw, scope=large_scope), checkpoint)

    async def test_acknowledgement_and_execution_entry_survive_reopen(self):
        pending = record()
        live = acknowledge_start(pending, admission())
        self.assertIs(live.local_phase, LocalExecutionPhase.NOT_ENTERED)
        self.assertIs(mark_execution_entered(live, PREPARED_IDENTITY).local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
        digest = EvidenceDigest.from_evidence(PREPARED_IDENTITY)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CheckpointStore(root, scope=scope(), limits=CheckpointLimits(1, 300000), operation_seconds=1)
            await store.open()
            try:
                await store.retain_start(pending)
                acknowledged = await store.acknowledge_start(pending, admission())
            finally:
                await store.close()
            store = CheckpointStore(root, scope=scope(), limits=CheckpointLimits(1, 300000), operation_seconds=1)
            await store.open()
            try:
                self.assertEqual(await store.inventory(), (acknowledged,))
                entered = await store.mark_entered(acknowledged, digest)
            finally:
                await store.close()
            store = CheckpointStore(root, scope=scope(), limits=CheckpointLimits(1, 300000), operation_seconds=1)
            await store.open()
            try:
                self.assertEqual(await store.inventory(), (entered,))
                self.assertEqual(entered.prepared, digest)
                self.assertEqual(entered.acknowledgement.start.body, pending.body)
                self.assertNotIn(PREPARED_IDENTITY.content, (root / "checkpoints.sqlite3").read_bytes())
            finally:
                await store.close()

    async def test_terminal_start_replay_is_retained_without_local_execution(self):
        for state in ("succeeded", "failed", "expired"):
            live = acknowledge_start(record(), admission(state=state, replayed=True))
            acknowledged = AcknowledgedCheckpoint(live.start, live.admission)
            restored = decode_checkpoint(encode_checkpoint(acknowledged), scope=scope())
            self.assertEqual(restored.admission.state, state)
            self.assertIs(live.local_phase, LocalExecutionPhase.NOT_ENTERED)
            with self.assertRaises(ValueError):
                mark_execution_entered(live, PREPARED_IDENTITY)
            with self.assertRaises(ValueError):
                EnteredCheckpoint(restored, EvidenceDigest.from_evidence(PREPARED_IDENTITY))
