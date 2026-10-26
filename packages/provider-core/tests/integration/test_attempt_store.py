"""Separate processes cannot concurrently own one producer checkpoint database."""

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, EvidenceDigest, encode_checkpoint
from provider_core.checkpoint_store import CheckpointFailed, CheckpointLimits, CheckpointRefused, CheckpointStore
from tests.fakes.attempt_store import PREPARED_IDENTITY, admission, record, scope


class CheckpointStoreProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_exit_preserves_entered_record_without_repeating_entry(self):
        child = """
import asyncio, json, os, sys
from pathlib import Path
from provider_core.attempt_record import RecoveryScope
from provider_core.producer_checkpoint import decode_checkpoint
from provider_core.checkpoint_store import CheckpointFailed, CheckpointLimits, CheckpointRefused, CheckpointStore

async def main():
    scope = RecoveryScope(**json.loads(sys.argv[2]))
    pending, acknowledged, entered = [decode_checkpoint(line, scope=scope) for line in sys.stdin.buffer]
    store = CheckpointStore(Path(sys.argv[1]), scope=scope,
                         limits=CheckpointLimits(1, 300000), operation_seconds=1)
    await store.open()
    await store.retain_start(pending)
    await store.acknowledge_start(pending, acknowledged.admission)
    await store.mark_entered(acknowledged, entered.prepared)
    os._exit(0)

asyncio.run(main())
"""
        pending = record()
        acknowledged = AcknowledgedCheckpoint(pending, admission())
        entered = EnteredCheckpoint(acknowledged, EvidenceDigest.from_evidence(PREPARED_IDENTITY))
        with tempfile.TemporaryDirectory() as directory:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-I", "-B", "-c", child, directory, json.dumps(asdict(scope()))],
                input=b"\n".join(encode_checkpoint(value) for value in (pending, acknowledged, entered)) + b"\n",
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            store = CheckpointStore(Path(directory), scope=scope(), limits=CheckpointLimits(1, 300000), operation_seconds=1)
            await store.open()
            try:
                (restored,) = await store.inventory()
                self.assertEqual(restored, entered)
                with self.assertRaises(CheckpointRefused):
                    await store.mark_entered(acknowledged, entered.prepared)
            finally:
                await store.close()

    async def test_second_process_is_excluded_then_recovers_after_owner_closes(self):
        child = """
import asyncio, json, sys
from pathlib import Path
from provider_core.attempt_record import RecoveryScope
from provider_core.checkpoint_store import CheckpointFailed, CheckpointLimits, CheckpointRefused, CheckpointStore

async def main():
    store = CheckpointStore(Path(sys.argv[1]), scope=RecoveryScope(**json.loads(sys.argv[2])),
                         limits=CheckpointLimits(2, 300000), operation_seconds=1)
    try:
        try:
            await store.open()
        except CheckpointFailed as failure:
            if not isinstance(failure.cause, BlockingIOError):
                raise
            print('locked')
        else:
            print(len(await store.inventory()))
    finally:
        await store.close()

asyncio.run(main())
"""
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(Path(directory), scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
            arguments = [sys.executable, "-I", "-B", "-c", child, directory, json.dumps(asdict(scope()))]
            await store.open()
            try:
                await store.retain_start(record())
                denied = await asyncio.to_thread(subprocess.run, arguments, capture_output=True, text=True, timeout=5)
                self.assertEqual(denied.returncode, 0, denied.stderr)
                self.assertEqual(denied.stdout, "locked\n")
            finally:
                await store.close()
            recovered = await asyncio.to_thread(subprocess.run, arguments, capture_output=True, text=True, timeout=5)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(recovered.stdout, "1\n")
