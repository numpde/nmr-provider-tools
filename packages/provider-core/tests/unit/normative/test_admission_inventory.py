"""Reading compact control inventory does not consume admission capacity."""

import tempfile
import unittest
from pathlib import Path

from provider_core.checkpoint_store import CheckpointLimits, CheckpointStore
from tests.fakes.attempt_store import record, scope


class AdmissionInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_returns_exact_control_without_consuming_a_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory), scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
            await store.open()
            try:
                self.assertEqual(await store.inventory(owner=self), ())
                await store.retain_start(record())
                self.assertEqual(await store.inventory(owner=self), (record(),))
                self.assertEqual(await store.inventory(owner=self), (record(),))
                await store.retain_start(record("retained:two"))
                self.assertCountEqual(await store.inventory(), (record(), record("retained:two")))
            finally:
                await store.close()
