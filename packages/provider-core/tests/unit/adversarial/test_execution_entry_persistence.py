"""A slow original disk write must not postpone stopping a prepared session."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint, SuppressedCheckpoint, TerminalSelectedCheckpoint, EvidenceDigest
import asyncio
import tempfile
import unittest
from contextlib import suppress
from unittest.mock import patch

from provider_core.api import ApiFailure
from provider_core.attempt_record import LocalExecutionPhase
from provider_core.checkpoint_store import CheckpointFailed
from provider_core.execution import AttemptExecutor, ExecutionFailed, ExecutionStopped
from provider_core.ownership import OwnershipRefused
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.entry_write import EntryWriteBarrier
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.api import api


def entry_replies(last):
    return (reply(observation()), reply(response()), reply(progress_response()),
            reply(observation()), reply(progress_response(phase="running")), last)


class EntryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_poll_keeps_api_and_original_disk_write_owned_until_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            reading = asyncio.Event()

            class PendingTransport(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    if len(self.calls) == 5:
                        self.calls.append((request, maximum_bytes))
                        self.pending_owner = owner
                        self.retirement_confirmed = False
                        reading.set()
                        await asyncio.Future()
                    return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

            transport = PendingTransport(reply(observation()), reply(response()), reply(progress_response()),
                                         reply(observation()), reply(progress_response(phase="running")))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                                       delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=0.01, task_settle_seconds=0.05)
            barrier = EntryWriteBarrier(store._database.mark_entered)
            task = None
            try:
                with patch.object(store._database, "mark_entered", side_effect=barrier.write):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                    await asyncio.wait_for(reading.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIsInstance(executor.last_failure.cause, asyncio.CancelledError)
                    self.assertIs(transport.pending_owner, executor)
                    self.assertIs(store.pending_owner, executor)
                    self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    barrier.release.set()
                    self.assertIsNone(await executor.settle())
                self.assertIsNone(transport.pending_owner)
                self.assertIsNone(store.pending_owner)
                self.assertEqual(barrier.calls, 1)
                self.assertEqual(len(transport.calls), 6)
                self.assertIs(type((await store.inventory())[0]), EnteredCheckpoint)
                claim.release()
            finally:
                barrier.release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if store.pending_owner is executor or transport.pending_owner is executor:
                    await executor.settle()
                await store.close()

    async def test_completed_write_during_poll_cannot_override_stop_or_hide_a_disk_failure(self):
        cases = ((reply(observation(job_state="closed")), ExecutionStopped, None),
                 (ExchangeFailed("private poll failure"), ApiFailure, None),
                 (reply(observation(state="expired")), ExecutionStopped, OSError("private persistence cause")))
        for last, cause_type, disk_failure in cases:
            with self.subTest(cause=cause_type, disk_failure=disk_failure):
                await self._check_completed_write_during_poll(last, cause_type, disk_failure)

    async def _check_completed_write_during_poll(self, last, cause_type, disk_failure):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            reading, release_poll, write_observed = asyncio.Event(), asyncio.Event(), asyncio.Event()

            class HeldPollTransport(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    if len(self.calls) == 5:
                        reading.set()
                        await release_poll.wait()
                    return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

            transport = HeldPollTransport(*entry_replies(last))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                                       delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=0.01, task_settle_seconds=0.05)
            original_replace = store.mark_entered

            async def observe_write(*args, **kwargs):
                try:
                    return await original_replace(*args, **kwargs)
                finally:
                    write_observed.set()

            barrier = EntryWriteBarrier(store._database.mark_entered)
            barrier.failure = disk_failure
            task = None
            try:
                with (patch.object(store._database, "mark_entered", side_effect=barrier.write),
                      patch.object(store, "mark_entered", side_effect=observe_write)):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                    await asyncio.wait_for(reading.wait(), 1)
                    barrier.release.set()
                    await asyncio.wait_for(write_observed.wait(), 1)
                    self.assertIsNone(store.pending_owner)
                    release_poll.set()
                    with self.assertRaises(ExecutionFailed) as failed:
                        await task
                self.assertIsInstance(failed.exception.cause, cause_type)
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                self.assertEqual(barrier.calls, 1)
                self.assertEqual(len(transport.calls), 6)
                if disk_failure is None:
                    self.assertIs(type((await store.inventory())[0]), EnteredCheckpoint)
                    self.assertEqual(failed.exception.cleanup_failures, ())
                    claim.release()
                else:
                    self.assertTrue(any(isinstance(cause, CheckpointFailed) for cause in failed.exception.cleanup_failures))
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
            finally:
                barrier.release.set()
                release_poll.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if store.pending_owner is executor:
                    with suppress(ExecutionFailed):
                        await executor.settle()
                await store.close()

    async def test_stop_or_failed_poll_retires_before_disk_release_and_settles_once(self):
        for last, cause_type in ((reply(observation(job_state="cancelled")), ExecutionStopped),
                                 (ExchangeFailed("private poll failure"), ApiFailure)):
            with self.subTest(cause=cause_type), tempfile.TemporaryDirectory() as directory:
                store, acknowledged, claim = await acknowledged_store(directory)
                session = Session(store)
                transport = Transport(*entry_replies(last))
                executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=0.01, task_settle_seconds=0.05)
                barrier = EntryWriteBarrier(store._database.mark_entered)
                task = None
                try:
                    with patch.object(store._database, "mark_entered", side_effect=barrier.write):
                        task = asyncio.create_task(executor.run(acknowledged))
                        self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                        done, _ = await asyncio.wait({task}, timeout=1)
                        self.assertTrue(done, "Stop must retire the prepared session while the original disk write is still blocked")
                        with self.assertRaises(ExecutionFailed) as failed:
                            await task
                        self.assertIsInstance(failed.exception.cause, cause_type)
                        self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                        self.assertFalse(barrier.release.is_set())
                        self.assertIs(store.pending_owner, executor)
                        with self.assertRaises(OwnershipRefused):
                            claim.release()
                        barrier.release.set()
                        self.assertIsNone(await executor.settle())
                    (retained,) = await store.inventory()
                    self.assertIs(type(retained), EnteredCheckpoint)
                    self.assertEqual(barrier.calls, 1)
                    self.assertEqual(len(transport.calls), 6)
                    self.assertIsInstance(executor.last_failure.cause, cause_type)
                    claim.release()
                finally:
                    barrier.release.set()
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    if store.pending_owner is executor:
                        await executor.settle()
                    await store.close()

    async def test_late_disk_failure_is_secondary_to_stop_and_failed_retirement_blocks_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            retirement = RuntimeError("private retirement cause")
            session.retirement_failure = retirement
            transport = Transport(*entry_replies(reply(observation(state="expired"))))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                                       delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=0.01, task_settle_seconds=0.05)
            barrier = EntryWriteBarrier(store._database.mark_entered)
            barrier.failure = OSError("private persistence cause")
            task = None
            try:
                with patch.object(store._database, "mark_entered", side_effect=barrier.write):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                    done, _ = await asyncio.wait({task}, timeout=1)
                    self.assertTrue(done, "Expiry observation must not wait for disk completion")
                    with self.assertRaises(ExecutionFailed) as failed:
                        await task
                    primary = failed.exception.cause
                    self.assertIsInstance(primary, ExecutionStopped)
                    self.assertIn(retirement, failed.exception.cleanup_failures)
                    barrier.release.set()
                    with self.assertRaises(ExecutionFailed):
                        await executor.settle()
                self.assertIs(executor.last_failure.cause, primary)
                self.assertTrue(any(isinstance(cause, CheckpointFailed) for cause in executor.last_failure.cleanup_failures))
                self.assertNotIn("private", str(executor.last_failure))
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                self.assertEqual(barrier.calls, 1)
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire", "retire"])
            finally:
                barrier.release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if store.pending_owner is executor:
                    with suppress(ExecutionFailed):
                        await executor.settle()
                await store.close()
