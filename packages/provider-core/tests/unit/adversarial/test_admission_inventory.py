"""A failed or unresolved inventory read grants no fresh control operation."""

import asyncio
import errno
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.checkpoint_store import CheckpointFailed, CheckpointLimits, CheckpointRefused, CheckpointStore
from tests.fakes.attempt_store import record, scope


class AdmissionInventoryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_inventory_observation_keeps_the_private_original_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory), scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
            await store.open()
            try:
                await store.retain_start(record())
                cause = OSError(errno.EIO, "PRIVATE inventory read")
                with patch.object(store._database, "inventory", side_effect=cause):
                    with self.assertRaises(CheckpointFailed) as caught:
                        await store.inventory(owner=self)
                self.assertIs(caught.exception.cause, cause)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(await store.inventory(), (record(),))
            finally:
                await store.close()

    async def test_cancelled_inventory_retains_original_io_until_exact_owner_settles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory), scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
            await store.open()
            entered, release = threading.Event(), threading.Event()
            original = store._database.inventory

            def held():
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("Fixture inventory read was not released")
                return original()

            try:
                with patch.object(store._database, "inventory", side_effect=held) as read:
                    task = asyncio.create_task(store.inventory(owner=self))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertIs(store.pending_owner, self)
                        with self.assertRaises(CheckpointRefused):
                            await store.retain_start(record())
                        with self.assertRaises(CheckpointRefused):
                            await store.close()
                        with self.assertRaises(CheckpointRefused):
                            await store.settle(owner=object())
                    finally:
                        release.set()
                    settled = await store.settle(owner=self)
                    self.assertEqual((settled.operation, settled.value), ("read producer checkpoints", ()))
                    self.assertEqual(read.call_count, 1)
                self.assertEqual(await store.inventory(), ())
            finally:
                release.set()
                if store.pending_operation is not None:
                    await store.settle(owner=self)
                await store.close()
