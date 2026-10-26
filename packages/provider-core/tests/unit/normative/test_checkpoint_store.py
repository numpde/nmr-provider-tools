"""Durable producer decisions survive restart without retaining scientific data."""

import asyncio
import errno
import hashlib
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from provider_core.checkpoint_store import CheckpointFailed, CheckpointLimits, CheckpointRefused, CheckpointStore
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint, EvidenceDigest
from provider_core.attempt_record import ProtectedEvidence
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.delivery import outcome


class CheckpointStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = self.make_store()
        await self.store.open()

    # silent-defaults: allow-contract -- The ordinary disk operation has a finite budget; expiry tests override it.
    def make_store(self, *, seconds=1):
        return CheckpointStore(self.root, scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=seconds)

    async def asyncTearDown(self):
        if self.store.pending_operation is not None:
            await self.store.settle(owner=self.store.pending_owner)
        await self.store.close()
        self.directory.cleanup()

    async def acknowledge(self):
        pending = record()
        await self.store.retain_start(pending)
        return await self.store.acknowledge_start(pending, admission())

    async def test_start_and_acknowledgement_survive_reopen_as_metadata(self):
        pending = record()
        self.assertEqual(await self.store.retain_start(pending), pending)
        self.assertEqual(await self.store.retain_start(pending), pending)
        self.assertEqual(await self.store.require_start(pending), pending)
        acknowledged = await self.store.acknowledge_start(pending, admission())
        await self.store.close()
        self.store = self.make_store()
        await self.store.open()
        self.assertEqual(await self.store.inventory(), (acknowledged,))
        self.assertEqual(acknowledged.start.body, pending.body)

    async def test_execution_selection_and_delivery_keep_digests_only_then_delete(self):
        acknowledged = await self.acknowledge()
        evidence = ProtectedEvidence("fixture.prepared.v1", b"PRIVATE_PREPARED_COORDINATES")
        entered = await self.store.mark_entered(acknowledged, EvidenceDigest.from_evidence(evidence))
        item = replace(outcome(provider="provider:example", suffix="a"), job_ref="job:example")
        selected = await self.store.select_terminal(entered, item)
        self.assertEqual(selected.body_sha256, "sha256:" + hashlib.sha256(item.body).hexdigest())
        self.assertEqual(selected.body_bytes, len(item.body))
        stored = (self.root / "checkpoints.sqlite3").read_bytes()
        self.assertNotIn(evidence.content, stored)
        self.assertNotIn(item.body, stored)
        accepted = await self.store.accept_delivery(selected, item.acceptance())
        self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
        await self.store.clear_after_cleanup(accepted)
        self.assertEqual(await self.store.inventory(), ())
        await self.store.close()
        self.assertEqual(os.stat(self.root / "checkpoints.sqlite3").st_mode & 0o777, 0o600)
        raw = (self.root / "checkpoints.sqlite3").read_bytes()
        for needle in (evidence.content, b"SCIENTIFIC_PAYLOAD_NEEDLE", b"retained:one"):
            self.assertNotIn(needle, raw)
        self.assertFalse((self.root / "checkpoints.sqlite3-journal").exists())
        self.assertEqual({path.name for path in self.root.iterdir()}, {"checkpoints.sqlite3", ".checkpoint-owner.lock"})

    async def test_cancelled_write_is_settled_once_before_reuse(self):
        owner = object()
        entered, release = threading.Event(), threading.Event()
        original = self.store._database.retain_start

        def delayed(pending):
            entered.set()
            release.wait(3)
            return original(pending)

        with patch.object(self.store._database, "retain_start", side_effect=delayed) as write:
            task = asyncio.create_task(self.store.retain_start(record(), owner=owner))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(CheckpointRefused):
                    await self.store.inventory()
                with self.assertRaises(CheckpointRefused):
                    await self.store.close()
            finally:
                release.set()
            settled = await self.store.settle(owner=owner)
            self.assertEqual(settled.value, record())
            self.assertEqual(write.call_count, 1)
        self.assertEqual(await self.store.inventory(), (record(),))

    async def test_expired_wait_keeps_the_original_commit_and_owner(self):
        self.store._seconds = 0.01
        release = threading.Event()
        original = self.store._database.retain_start
        owner = object()

        def delayed(pending):
            release.wait(3)
            return original(pending)

        with patch.object(self.store._database, "retain_start", side_effect=delayed) as write:
            try:
                with self.assertRaises(CheckpointFailed):
                    await self.store.retain_start(record(), owner=owner)
                with self.assertRaises(CheckpointRefused):
                    await self.store.settle(owner=object())
            finally:
                release.set()
                self.store._seconds = 1
            self.assertEqual((await self.store.settle(owner=owner)).value, record())
            self.assertEqual(write.call_count, 1)

    async def test_selected_identity_survives_restart_without_becoming_accepted(self):
        ack = await self.acknowledge()
        item = replace(outcome(provider="provider:example", suffix="a"), job_ref="job:example")
        selected = await self.store.select_terminal(ack, item)
        await self.store.close()
        self.store = self.make_store()
        await self.store.open()
        self.assertEqual(await self.store.inventory(), (selected,))
        self.assertNotIsInstance(selected, DeliveryAcceptedCheckpoint)

    async def test_failed_disk_operation_reaches_the_protected_renderer_without_raw_text(self):
        from provider_core.diagnostics import failure_evidence
        import json
        cause = OSError(errno.EIO, "PRIVATE_DISK_EXCEPTION")
        with patch.object(self.store._database, "retain_start", side_effect=cause):
            with self.assertRaises(CheckpointFailed) as failed:
                await self.store.retain_start(record())
        report = json.dumps(failure_evidence(failed.exception, secrets=()))
        self.assertIn("Cannot retain pending START checkpoint", report)
        self.assertIn('"errno": 5', report)
        self.assertNotIn("PRIVATE_DISK_EXCEPTION", report)
