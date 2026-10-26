"""Scientific acceptance transfers bytes while native cleanup proceeds independently."""

import asyncio
import errno
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from provider_core.execution import AttemptExecutor, ExecutionFailed
from provider_core.diagnostics import failure_evidence
from provider_core.delivery_store import DeliveryFull
from provider_core.ownership import OwnershipRefused
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint, EnteredCheckpoint, EvidenceDigest
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.checkpoint_execution import CheckpointSession, RecordingDelivery, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.api import api


def success_transport():
    return Transport(reply(observation()), reply(response()), reply(progress_response()), reply(observation()),
                     reply(progress_response(phase="running")), reply(observation()), reply(observation()))


def executor_at(store, acknowledged, claim, session, delivery):
    return AttemptExecutor(api(success_transport(), store.scope.api_origin), store, claim, session,
                           delivery=delivery, on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
                           delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
                           poll_seconds=0.01, task_settle_seconds=1)


class FullUntilReleased(RecordingDelivery):
    def __init__(self):
        super().__init__()
        self.full_seen, self.release = asyncio.Event(), asyncio.Event()
        self.attempted = []

    async def submit(self, item):
        self.attempted.append(item.body)
        if not self.release.is_set():
            self.full_seen.set()
            raise DeliveryFull("Delivery capacity is full.")
        return await super().submit(item)


class DeliveryExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_transfers_exact_result_and_retains_only_compact_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = CheckpointSession(store), RecordingDelivery()
            executor = executor_at(store, acknowledged, claim, session, delivery)
            try:
                accepted = await executor.run(acknowledged)
                self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
                self.assertIs(type(session.events[1][1]), EnteredCheckpoint)
                self.assertEqual(session.events[1][1].prepared, EvidenceDigest.from_evidence(session.handle.identity))
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(len(delivery.items), 1)
                self.assertEqual(delivery.items[0].body_sha256, accepted.selection.body_sha256)
                self.assertEqual(await store.inventory(), (accepted,))
                self.assertIsNone(executor._outcome)
                self.assertIsNone(executor._delivery_outcome)
                self.assertTrue(executor.retirement_confirmed)
                claim.release()
            finally:
                await store.close()

    async def test_blocked_selection_never_blocks_cleanup_or_submits_early(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = CheckpointSession(store), RecordingDelivery()
            executor = executor_at(store, acknowledged, claim, session, delivery)
            entered, release = threading.Event(), threading.Event()
            original = store._database.select_terminal

            def delayed(*args):
                entered.set()
                release.wait(3)
                return original(*args)

            task = None
            try:
                with patch.object(store._database, "select_terminal", side_effect=delayed):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    await asyncio.wait_for(session.retirement_started.wait(), 1)
                    self.assertEqual(delivery.items, [])
                    release.set()
                    await task
                claim.release()
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_failed_accepted_metadata_write_never_retains_or_resubmits_body(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = CheckpointSession(store), RecordingDelivery()
            executor = executor_at(store, acknowledged, claim, session, delivery)
            try:
                with patch.object(store._database, "accept_delivery", side_effect=OSError("private disk failure")):
                    with self.assertRaises(ExecutionFailed) as failed:
                        await executor.run(acknowledged)
                self.assertEqual(len(delivery.items), 1)
                self.assertIsNone(executor._outcome)
                self.assertIsNone(executor._delivery_outcome)
                self.assertIsNone(failed.exception.outcome)
                report = json.dumps(failure_evidence(failed.exception, secrets=()))
                self.assertIn('"delivery_acceptance_received": true', report)
                with self.assertRaises(ExecutionFailed):
                    await executor.settle()
                self.assertEqual(len(delivery.items), 1)
            finally:
                await store.close()

    async def test_full_queue_paces_the_same_body_while_cleanup_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = CheckpointSession(store), FullUntilReleased()
            executor = executor_at(store, acknowledged, claim, session, delivery)
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(delivery.full_seen.wait(), 1)
                await asyncio.wait_for(session.retirement_started.wait(), 1)
                self.assertFalse(task.done())
                delivery.release.set()
                await asyncio.wait_for(task, 1)
                self.assertGreaterEqual(len(delivery.attempted), 2)
                self.assertEqual(set(delivery.attempted), {delivery.items[0].body})
                self.assertEqual([event[0] for event in session.events].count("execute"), 1)
                self.assertGreaterEqual(executor._on_delivery_retry.call_count, 1)
                claim.release()
            finally:
                delivery.release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_cleanup_failure_keeps_relay_acceptance_without_another_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            delivery = RecordingDelivery()

            class FailAfterAcceptance(CheckpointSession):
                async def retire(self):
                    await delivery.accepted.wait()
                    await super().retire()

            session = FailAfterAcceptance(store)
            session.retirement_failure = OSError("native cleanup failed")
            executor = executor_at(store, acknowledged, claim, session, delivery)
            try:
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIsNotNone(failed.exception.delivery_acceptance)
                self.assertIsNone(failed.exception.outcome)
                self.assertEqual(len(delivery.items), 1)
                self.assertFalse(executor.retirement_confirmed)
                session.retirement_failure = None
                self.assertIs(type(await executor.settle()), DeliveryAcceptedCheckpoint)
                self.assertEqual(len(delivery.items), 1)
                self.assertEqual([event[0] for event in session.events].count("execute"), 1)
                claim.release()
            finally:
                await store.close()

    async def test_cleanup_failure_preserves_original_backpressure_until_capacity_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            delivery = FullUntilReleased()

            class FailAfterFull(CheckpointSession):
                async def retire(self):
                    await delivery.full_seen.wait()
                    await super().retire()

            session = FailAfterFull(store)
            session.retirement_failure = OSError(errno.EIO, "PRIVATE_NATIVE")
            executor = executor_at(store, acknowledged, claim, session, delivery)
            try:
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertGreater(len(delivery.attempted), 0)
                report = json.dumps(failure_evidence(failed.exception, secrets=()))
                self.assertIn('"errno": 5', report)
                self.assertNotIn("PRIVATE_NATIVE", report)
                self.assertGreater(executor._on_delivery_retry.call_count, 0)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                delivery.release.set()
                await asyncio.wait_for(asyncio.shield(executor._handoff_wait), 1)
                session.retirement_failure = None
                self.assertIs(type(await executor.settle()), DeliveryAcceptedCheckpoint)
                self.assertIsNone(executor._outcome)
                self.assertEqual(len(delivery.items), 1)
                self.assertEqual(set(delivery.attempted), {delivery.items[0].body})
                self.assertEqual([event[0] for event in session.events].count("execute"), 1)
                claim.release()
            finally:
                delivery.release.set()
                if executor._handoff_wait is not None:
                    await asyncio.wait_for(asyncio.shield(executor._handoff_wait), 1)
                await store.close()

    async def test_cleanup_failure_during_selection_does_not_veto_approved_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            fail_cleanup = asyncio.Event()
            cleanup_failed = asyncio.Event()

            class ControlledFailure(CheckpointSession):
                async def retire(self):
                    await fail_cleanup.wait()
                    cleanup_failed.set()
                    raise OSError(errno.EIO, "PRIVATE_NATIVE")

            session, delivery = ControlledFailure(store), RecordingDelivery()
            executor = executor_at(store, acknowledged, claim, session, delivery)
            entered, release = threading.Event(), threading.Event()
            original = store._database.select_terminal

            def delayed(*args):
                entered.set()
                release.wait(3)
                return original(*args)

            task = None
            try:
                with patch.object(store._database, "select_terminal", side_effect=delayed):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    fail_cleanup.set()
                    await asyncio.wait_for(cleanup_failed.wait(), 1)
                    release.set()
                    with self.assertRaises(ExecutionFailed):
                        await task
                self.assertEqual(len(delivery.items), 1)
                self.assertIsNone(executor._outcome)
                self.assertIs(type((await store.inventory())[0]), DeliveryAcceptedCheckpoint)
                self.assertFalse(executor.retirement_confirmed)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
            finally:
                fail_cleanup.set()
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_relay_acceptance_does_not_wait_for_native_cleanup(self):
        class PausedRetirement(CheckpointSession):
            def __init__(self, store):
                super().__init__(store)
                self.release = asyncio.Event()

            async def retire(self):
                self.retirement_started.set()
                await self.release.wait()
                await super().retire()

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = PausedRetirement(store), RecordingDelivery()
            executor = executor_at(store, acknowledged, claim, session, delivery)
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(delivery.accepted.wait(), 1)
                self.assertFalse(task.done())
                self.assertFalse(executor.retirement_confirmed)
                self.assertIsNone(executor._outcome)
                session.release.set()
                await asyncio.wait_for(task, 1)
                claim.release()
            finally:
                session.release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_explicit_cancellation_stops_backpressure_without_resubmission_from_settle(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = CheckpointSession(store), FullUntilReleased()
            executor = executor_at(store, acknowledged, claim, session, delivery)
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(delivery.full_seen.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                count = len(delivery.attempted)
                delivery.release.set()
                with self.assertRaises(ExecutionFailed):
                    await executor.settle()
                self.assertEqual(len(delivery.attempted), count)
                self.assertEqual(delivery.items, [])
                self.assertIsNotNone(executor._outcome)
            finally:
                delivery.release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_native_cleanup_cancellation_does_not_withdraw_approved_handoff(self):
        class CancelledCleanup(CheckpointSession):
            async def retire(self):
                raise asyncio.CancelledError("native cleanup interrupted")

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = CancelledCleanup(store), RecordingDelivery()
            executor = executor_at(store, acknowledged, claim, session, delivery)
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await executor.run(acknowledged)
                self.assertEqual(len(delivery.items), 1)
                self.assertIsNone(executor._outcome)
                self.assertFalse(executor.retirement_confirmed)
                self.assertIsNotNone(executor.last_failure.delivery_acceptance)
                self.assertTrue(executor.last_failure.cleanup_failures)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
            finally:
                await store.close()
