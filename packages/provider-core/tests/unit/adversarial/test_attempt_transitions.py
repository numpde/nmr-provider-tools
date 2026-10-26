"""Stale, foreign or backward transitions cannot replace retained obligations."""

import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from provider_core.attempt_record import acknowledge_start, mark_execution_entered
from provider_core.checkpoint_store import CheckpointLimits, CheckpointRefused, CheckpointStore
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, EnteredCheckpoint, EvidenceDigest, decode_checkpoint, encode_checkpoint,
)
from tests.fakes.attempt_store import PREPARED_IDENTITY, admission, record, scope


class AttemptTransitionFailureTests(unittest.IsolatedAsyncioTestCase):
    def store(self, root):
        return CheckpointStore(root, scope=scope(), limits=CheckpointLimits(1, 300000), operation_seconds=1)

    async def test_foreign_acknowledgement_and_repeated_entry_are_rejected(self):
        for changes in (
            {"job_ref": "job:foreign"},
            {"provider_ref": "provider:foreign"},
            {"analysis_kind_ref": "different_kind"},
            {"execution_attempt_ref": "private-invalid-reference"},
        ):
            with self.assertRaises(ValueError):
                acknowledge_start(record(), replace(admission(), **changes))
        with self.assertRaises(ValueError):
            mark_execution_entered(
                mark_execution_entered(acknowledge_start(record(), admission()), PREPARED_IDENTITY), PREPARED_IDENTITY
            )

    async def test_stale_absent_foreign_and_backward_transitions_preserve_current_control(self):
        pending = record()
        digest = EvidenceDigest.from_evidence(PREPARED_IDENTITY)
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory))
            await store.open()
            try:
                with self.assertRaises(CheckpointRefused):
                    await store.acknowledge_start(pending, admission())
                await store.retain_start(pending)
                with self.assertRaises(CheckpointRefused):
                    await store.mark_entered(pending, digest)
                with self.assertRaises((ValueError, CheckpointRefused)):
                    await store.acknowledge_start(record("another:key"), admission())
                self.assertEqual(await store.inventory(), (pending,))
                acknowledged = await store.acknowledge_start(pending, admission())
                with self.assertRaises(CheckpointRefused):
                    await store.acknowledge_start(pending, admission())
                entered = await store.mark_entered(acknowledged, digest)
                with self.assertRaises(CheckpointRefused):
                    await store.mark_entered(acknowledged, digest)
                with self.assertRaises(CheckpointRefused):
                    await store.retain_start(pending)
                self.assertEqual(await store.inventory(), (entered,))
            finally:
                await store.close()

    async def test_corrupt_checkpoints_cannot_supply_active_control(self):
        acknowledged = AcknowledgedCheckpoint(record(), admission())
        good = json.loads(encode_checkpoint(acknowledged))
        for changes in (
            {"kind": "private-canary"}, {"admission": {}}, {"start": {}}, {"extra": "private-canary"},
        ):
            with self.assertRaises(ValueError) as caught:
                decode_checkpoint(json.dumps(good | changes).encode(), scope=scope())
            self.assertNotIn("private-canary", str(caught.exception))
        entered = EnteredCheckpoint(acknowledged, EvidenceDigest.from_evidence(PREPARED_IDENTITY))
        document = json.loads(encode_checkpoint(entered))
        document["acknowledgement"]["admission"]["state"] = "succeeded"
        with self.assertRaises(ValueError):
            decode_checkpoint(json.dumps(document).encode(), scope=scope())

    async def test_cancelled_acknowledgement_settles_once_before_storage_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory))
            pending = record()
            await store.open()
            await store.retain_start(pending)
            reached, release = threading.Event(), threading.Event()
            original = store._database.acknowledge_start

            def delayed_acknowledgement(*args):
                reached.set()
                if not release.wait(3):
                    raise TimeoutError("Fixture acknowledgement was not released")
                return original(*args)

            try:
                with patch.object(store._database, "acknowledge_start", side_effect=delayed_acknowledgement) as writes:
                    task = asyncio.create_task(store.acknowledge_start(pending, admission(), owner=self))
                    try:
                        self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        with self.assertRaises(CheckpointRefused):
                            await store.acknowledge_start(pending, admission())
                        with self.assertRaises(CheckpointRefused):
                            await store.close()
                    finally:
                        release.set()
                    acknowledged = (await store.settle(owner=self)).value
                    self.assertEqual(writes.call_count, 1)
                self.assertEqual(await store.inventory(), (acknowledged,))
                self.assertEqual(acknowledged.start.body, pending.body)
                self.assertEqual(acknowledged.admission, admission())
            finally:
                release.set()
                if store.pending_operation is not None:
                    await store.settle(owner=self)
                await store.close()
