"""Server stop and completion races preserve both ownership and scientific evidence."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import SuppressedCheckpoint
from tests.fakes.execution import live_executor
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.attempt_record import SuppressionReason
from provider_core.execution import ExecutionFailed, ExecutionStopped
from provider_core.ownership import OwnershipRefused
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.execution import Session, live_attempt
from tests.fakes.job_input import response
from tests.fakes.api import api


class WaitingSession(Session):
    # silent-defaults: allow-contract -- The ordinary session honors cancellation; resist=True injects a retirement failure.
    def __init__(self, store, phase, *, resist=False):
        super().__init__(store)
        self.phase = phase
        self.resist = resist
        self.release = asyncio.Event()
        self.cancel_count = 0
        self.produced = asyncio.Event()

    async def _wait(self):
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_count += 1
            if not self.resist:
                raise
            await self.release.wait()

    async def prepare(self, acknowledged, instructions, *, report_condition):
        result = await super().prepare(acknowledged, instructions, report_condition=report_condition)
        if self.phase == "prepare":
            await self._wait()
        return result

    async def execute(self, prepared, *, report_condition):
        result = await super().execute(prepared, report_condition=report_condition)
        if self.phase == "execute":
            await self._wait()
        self.produced.set()
        return result


class ExecutionObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_each_phase_cancels_once_and_never_publishes(self):
        for phase in ("prepare", "execute"):
            with self.subTest(phase=phase):
                store, acknowledged, claim = await live_attempt()
                replies = [reply(observation()), reply(response()), reply(progress_response())]
                if phase == "execute":
                    replies += [reply(observation()), reply(progress_response(phase="running")), reply(observation())]
                replies.append(reply(observation(job_state="closed")))
                session = WaitingSession(store, phase)
                executor = live_executor(
                    api(Transport(*replies), store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=0.01,
                    task_settle_seconds=0.02,
                )
                try:
                    with self.assertRaises(ExecutionFailed) as failed:
                        await executor.run(acknowledged)
                    self.assertIsInstance(failed.exception.cause, ExecutionStopped)
                    self.assertEqual(session.cancel_count, 1)
                    # silent-defaults: allow-contract -- The canceled phase determines the already-observed event order.
                    self.assertEqual(
                        [e[0] for e in session.events],
                        ["prepare", "retire"] if phase == "prepare" else ["prepare", "execute", "retire"],
                    )
                    claim.release(owner=claim.lifecycle_owner)
                finally:
                    session.release.set()

    async def test_retired_worker_with_pending_task_keeps_claim_until_original_task_settles(self):
        store, acknowledged, claim = await live_attempt()
        session = WaitingSession(store, "prepare", resist=True)
        transport = Transport(
            reply(observation()), reply(response()), reply(progress_response()), reply(observation(job_state="closed"))
        )
        executor = live_executor(
            api(transport, store.scope.api_origin),
            store,
            claim,
            session,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
            poll_seconds=0.01,
            task_settle_seconds=0.01,
        )
        try:
            with self.assertRaises(ExecutionFailed) as failed:
                await executor.run(acknowledged)
            self.assertIsInstance(failed.exception.cause, ExecutionStopped)
            self.assertTrue(any(isinstance(e, TimeoutError) for e in failed.exception.cleanup_failures))
            with self.assertRaises(OwnershipRefused):
                claim.release(owner=claim.lifecycle_owner)
            with self.assertRaises(ExecutionFailed):
                await executor.settle()
            self.assertEqual(session.cancel_count, 1)
            session.release.set()
            self.assertIsNone(await executor.settle())
            self.assertEqual([e[0] for e in session.events], ["prepare", "retire"])
            self.assertEqual(len(transport.calls), 4)
            claim.release(owner=claim.lifecycle_owner)
        finally:
            session.release.set()

    async def test_final_stop_preserves_completed_result_without_terminal_publication(self):
        store, acknowledged, claim = await live_attempt()
        session = Session(store)
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            reply(progress_response(phase="running")),
            reply(observation()),
            reply(observation(job_state="closed")),
        )
        executor = live_executor(
            api(transport, store.scope.api_origin),
            store,
            claim,
            session,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
            poll_seconds=0.1,
            task_settle_seconds=0.02,
        )
        with self.assertRaises(ExecutionFailed) as failed:
            await executor.run(acknowledged)
        self.assertIsInstance(failed.exception.cause, ExecutionStopped)
        self.assertIs(failed.exception.outcome, session.outcome)
        with self.assertRaises(OwnershipRefused):
            claim.release(owner=claim.lifecycle_owner)
        with self.assertRaises(ExecutionFailed):
            await executor.settle()
        retained = await executor.retain_suppressed_disposition()
        self.assertIsInstance(retained, SuppressedCheckpoint)
        self.assertIs(retained.reason, SuppressionReason.SERVER_STOP)
        self.assertFalse(hasattr(retained, "candidate"))
        self.assertIsNone(executor._outcome)
        self.assertEqual(store.records, (retained,))
        claim.release(owner=claim.lifecycle_owner)
        self.assertEqual([e[0] for e in session.events], ["prepare", "execute", "retire"])

    async def test_result_during_failed_poll_is_preserved_with_original_api_failure(self):
        store, acknowledged, claim = await live_attempt()
        session = WaitingSession(store, "execute")

        class RacingTransport(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if len(self.calls) == 6:
                    session.release.set()
                    await session.produced.wait()
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        transport = RacingTransport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            reply(progress_response(phase="running")),
            reply(observation()),
            ExchangeFailed("poll reply lost"),
        )
        executor = live_executor(
            api(transport, store.scope.api_origin),
            store,
            claim,
            session,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
            poll_seconds=0.01,
            task_settle_seconds=0.02,
        )
        try:
            with self.assertRaises(ExecutionFailed) as failed:
                await executor.run(acknowledged)
            self.assertIs(failed.exception.cause, failed.exception.api_failure)
            self.assertIs(failed.exception.outcome, session.outcome)
            self.assertEqual(len(transport.calls), 7)
            with self.assertRaises(OwnershipRefused):
                claim.release(owner=claim.lifecycle_owner)
            retained = await executor.retain_suppressed_disposition()
            self.assertIs(retained.reason, SuppressionReason.OBSERVATION_FAILED)
            self.assertIs(executor.last_failure.cause, failed.exception.api_failure)
            self.assertEqual(store.records, (retained,))
            claim.release(owner=claim.lifecycle_owner)
            self.assertEqual([e[0] for e in session.events], ["prepare", "execute", "retire"])
        finally:
            session.release.set()

    async def test_late_execution_result_updates_failure_and_keeps_unretained_outcome_owned(self):
        store, acknowledged, claim = await live_attempt()
        session = WaitingSession(store, "execute", resist=True)
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            reply(progress_response(phase="running")),
            reply(observation()),
            reply(observation(job_state="closed")),
        )
        executor = live_executor(
            api(transport, store.scope.api_origin),
            store,
            claim,
            session,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
            poll_seconds=0.01,
            task_settle_seconds=0.01,
        )
        try:
            with self.assertRaises(ExecutionFailed):
                await executor.run(acknowledged)
            self.assertIsNone(executor.last_failure.outcome)
            session.release.set()
            await session.produced.wait()
            self.assertIs(executor.last_failure.outcome, session.outcome)
            with self.assertRaises(ExecutionFailed):
                await executor.settle()
            self.assertIs(executor.last_failure.outcome, session.outcome)
            self.assertEqual(session.cancel_count, 1)
            with self.assertRaises(OwnershipRefused):
                claim.release(owner=claim.lifecycle_owner)
            retained = await executor.retain_suppressed_disposition()
            self.assertIs(retained.reason, SuppressionReason.SERVER_STOP)
            self.assertEqual(store.records, (retained,))
            claim.release(owner=claim.lifecycle_owner)
            self.assertEqual(len(transport.calls), 7)
        finally:
            session.release.set()

    async def test_failed_binding_needs_no_final_acceptance_read(self):
        for phase in ("prepare", "execute"):
            with self.subTest(phase=phase):
                store, acknowledged, claim = await live_attempt()
                replies = [reply(observation()), reply(response()), reply(progress_response())]
                if phase == "execute":
                    replies += [reply(observation()), reply(progress_response(phase="running")), reply(observation())]
                transport = Transport(*replies, ExchangeFailed("must not read after binding failure"))
                session = Session(store)
                cause = RuntimeError("private binding failure")
                executor = live_executor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=0.1,
                    task_settle_seconds=0.02,
                )
                with patch.object(session, phase, AsyncMock(side_effect=cause)), self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIs(failed.exception.cause, cause)
                self.assertEqual(len(transport.calls), len(replies))
                self.assertEqual(failed.exception.cleanup_failures, ())
                claim.release(owner=claim.lifecycle_owner)

    async def test_cancellation_during_poll_retains_api_ownership_until_settlement(self):
        store, acknowledged, claim = await live_attempt()
        session = WaitingSession(store, "execute")
        reading = asyncio.Event()

        class PendingTransport(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if len(self.calls) == 6:
                    self.calls.append((request, maximum_bytes))
                    self.pending_owner = owner
                    self.retirement_confirmed = False
                    reading.set()
                    await asyncio.Future()
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        transport = PendingTransport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            reply(progress_response(phase="running")),
            reply(observation()),
        )
        executor = live_executor(
            api(transport, store.scope.api_origin),
            store,
            claim,
            session,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
            poll_seconds=0.01,
            task_settle_seconds=0.02,
        )
        task = asyncio.create_task(executor.run(acknowledged))
        try:
            await asyncio.wait_for(reading.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsInstance(executor.last_failure.cause, asyncio.CancelledError)
            self.assertIs(transport.pending_owner, executor)
            with self.assertRaises(OwnershipRefused):
                claim.release(owner=claim.lifecycle_owner)
            self.assertIsNone(await executor.settle())
            self.assertIsNone(transport.pending_owner)
            self.assertEqual(session.cancel_count, 1)
            self.assertEqual(len(transport.calls), 7)
            claim.release(owner=claim.lifecycle_owner)
        finally:
            session.release.set()
