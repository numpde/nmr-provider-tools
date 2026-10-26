"""Conflicting or foreign producer decisions cannot overwrite retained work."""

import tempfile
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path

from provider_core.checkpoint_store import CheckpointFailed, CheckpointLimits, CheckpointRefused, CheckpointStore
from provider_core.producer_checkpoint import encode_checkpoint
from provider_core.producer_checkpoint import EvidenceDigest
from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_request import complete_body
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.delivery import outcome


class CheckpointRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = CheckpointStore(self.root, scope=scope(), limits=CheckpointLimits(1, 150000), operation_seconds=1)
        await self.store.open()

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_advanced_start_cannot_be_redispatched_or_entered_twice(self):
        pending = record()
        await self.store.retain_start(pending)
        ack = await self.store.acknowledge_start(pending, admission())
        for operation in (self.store.retain_start, self.store.require_start):
            with self.assertRaises(CheckpointRefused):
                await operation(pending)
        digest = EvidenceDigest("prepared.v1", "sha256:" + "a" * 64, 1)
        entered = await self.store.mark_entered(ack, digest)
        with self.assertRaises(CheckpointRefused):
            await self.store.mark_entered(ack, digest)
        self.assertEqual(await self.store.inventory(), (entered,))

    async def test_wrong_scope_and_full_capacity_leave_original_start_unchanged(self):
        pending = record()
        await self.store.retain_start(pending)
        for other in (replace(pending, scope=replace(scope(), generation_id="generation:foreign")), record("retained:two")):
            with self.assertRaises(CheckpointRefused):
                await self.store.retain_start(other)
        self.assertEqual(await self.store.inventory(), (pending,))

    async def test_selected_identity_cannot_change_and_acceptance_must_match(self):
        pending = record()
        await self.store.retain_start(pending)
        ack = await self.store.acknowledge_start(pending, admission())
        item = replace(outcome(provider="provider:example", suffix="a"), job_ref="job:example")
        with self.assertRaises(CheckpointRefused):
            await self.store.select_terminal(ack, replace(item, job_ref="job:other"))
        success = replace(item, operation=TerminalOperation.COMPLETE,
                          body=complete_body(item.execution_attempt_ref, "fixture.result.v1", b"result bytes"))
        with self.assertRaises(CheckpointRefused):
            await self.store.select_terminal(ack, success)
        selected = await self.store.select_terminal(ack, item)
        with self.assertRaises(CheckpointRefused):
            await self.store.select_terminal(ack, replace(item, body=item.body + b" "))
        with self.assertRaises(CheckpointRefused):
            await self.store.accept_delivery(selected, {**item.acceptance(), "body_sha256": "sha256:" + "f" * 64})
        with self.assertRaises(CheckpointRefused):
            await self.store.clear_after_cleanup(selected)
        self.assertEqual(await self.store.inventory(), (selected,))

    async def test_second_owner_is_excluded_and_reopen_checks_total_metadata_bytes(self):
        pending = record()
        await self.store.retain_start(pending)
        other = CheckpointStore(self.root, scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
        try:
            with self.assertRaises(CheckpointFailed):
                await other.open()
        finally:
            await other.close()
        await self.store.close()
        self.store = CheckpointStore(self.root, scope=scope(), limits=CheckpointLimits(1, len(encode_checkpoint(pending)) - 1), operation_seconds=1)
        with self.assertRaises(CheckpointRefused):
            await self.store.open()
        # Refusal closes its acquired handles, leaving the original data intact.
        other = CheckpointStore(self.root, scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
        try:
            await other.open()
            self.assertEqual(await other.inventory(), (pending,))
        finally:
            await other.close()

    async def test_initial_schema_and_version_roll_back_together(self):
        await self.store.close()
        (self.root / "checkpoints.sqlite3").unlink()
        from unittest.mock import patch

        # Failure at the version statement must not leave an orphaned table
        # which a later legitimate open mistakes for another application.
        class InterruptVersion(sqlite3.Connection):
            def execute(self, statement, *args):
                if statement == "PRAGMA user_version=1":
                    raise sqlite3.OperationalError("version write interrupted")
                return super().execute(statement, *args)

        connect = sqlite3.connect
        def interrupted_connect(*args, **kwargs):
            return connect(*args, **kwargs, factory=InterruptVersion)

        self.store = CheckpointStore(self.root, scope=scope(), limits=CheckpointLimits(1, 150000), operation_seconds=1)
        with patch("provider_core.checkpoint_store.sqlite3.connect", side_effect=interrupted_connect):
            with self.assertRaises(CheckpointFailed):
                await self.store.open()
        await self.store.close()
        self.store = CheckpointStore(self.root, scope=scope(), limits=CheckpointLimits(1, 150000), operation_seconds=1)
        await self.store.open()
        self.assertEqual(await self.store.inventory(), ())
