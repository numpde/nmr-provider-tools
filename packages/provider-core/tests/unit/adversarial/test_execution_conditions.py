"""Expired or failed condition capabilities cannot authorize later effects."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint
from tests.fakes.execution import live_executor
import asyncio
import tempfile
import unittest

from provider_core.api import ApiFailure
from provider_core.execution import ExecutionFailed, ExecutionRefused
from provider_core.transport import ExchangeFailed
from tests.fakes.conditions import ConditionTransport, ReportingSession, SwallowingSession
from tests.fakes.execution import live_attempt
from tests.fakes.api import api


class PausingSession(ReportingSession):
    def __init__(self, store):
        super().__init__(store)
        self.conditions = {"prepare": (), "execute": ()}
        self.finish = asyncio.Event()
        self.returning = asyncio.Event()

    async def prepare(self, acknowledged, instructions, *, report_condition):
        result = await super().prepare(acknowledged, instructions, report_condition=report_condition)
        await self.finish.wait()
        self.returning.set()
        return result


class ExecutionConditionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_swallowed_cancellation_while_waiting_for_poll_cannot_authorize_execution(self):
        class QueuedSession(ReportingSession):
            async def report(self, _phase, report_condition):
                self.operation = asyncio.current_task()
                await start_report.wait()
                reporting.set()
                try:
                    await report_condition("waiting_capacity")
                except asyncio.CancelledError as cause:
                    self.caught = cause

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            transport = ConditionTransport()
            transport.block_read = 2
            start_report, reporting = asyncio.Event(), asyncio.Event()
            session = QueuedSession(store)
            executor = live_executor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.005,
                task_settle_seconds=0.1,
            )
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(transport.read_blocked.wait(), 1)
                start_report.set()
                await asyncio.wait_for(reporting.wait(), 1)
                session.operation.cancel()
                transport.release_read.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                self.assertIs(executor.last_failure.cause, session.caught)
                self.assertIs(executor.last_failure.operation_interruption, session.caught)
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                self.assertEqual(transport.maximum_in_flight, 1)
                self.assertFalse(any(b"waiting_capacity" in request.content for request, _ in transport.calls))
                claim.release(owner=claim.lifecycle_owner)
            finally:
                start_report.set()
                transport.release_read.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                pass

    async def test_report_queued_behind_poll_rechecks_phase_after_acquiring_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            transport = ConditionTransport()
            transport.block_read = 2
            session = PausingSession(store)
            executor = live_executor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.005,
                task_settle_seconds=0.1,
            )
            task = asyncio.create_task(executor.run(acknowledged))
            report = None
            try:
                await asyncio.wait_for(transport.read_blocked.wait(), 1)
                queued = asyncio.Event()

                async def report_late():
                    queued.set()
                    await session.reporters["prepare"]("late_condition")

                report = asyncio.create_task(report_late())
                await asyncio.wait_for(queued.wait(), 1)
                self.assertFalse(report.done())
                session.finish.set()
                await asyncio.wait_for(session.returning.wait(), 1)
                transport.release_read.set()
                with self.assertRaises(ExecutionRefused):
                    await report
                await asyncio.wait_for(task, 1)
                self.assertFalse(any(b"late_condition" in request.content for request, _ in transport.calls))
                self.assertEqual(transport.maximum_in_flight, 1)
                claim.release(owner=claim.lifecycle_owner)
            finally:
                transport.release_read.set()
                session.finish.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if report is not None:
                    await asyncio.gather(report, return_exceptions=True)
                pass

    async def test_retained_phase_reporters_refuse_without_api_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            transport = ConditionTransport()
            transport.release.set()
            session = ReportingSession(store)
            executor = live_executor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            await executor.run(acknowledged)
            count = len(transport.calls)
            for reporter in session.reporters.values():
                with self.assertRaises(ExecutionRefused):
                    await reporter("late_condition")
            self.assertEqual(len(transport.calls), count)
            claim.release(owner=claim.lifecycle_owner)

    async def test_invalid_condition_uses_existing_request_admission_and_never_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            transport = ConditionTransport()
            session = ReportingSession(store)
            session.conditions["prepare"] = ("PRIVATE invalid text",)
            with self.assertRaises(ExecutionFailed) as failed:
                await live_executor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                ).run(acknowledged)
            self.assertIsInstance(failed.exception.cause, ValueError)
            self.assertNotIn("PRIVATE", str(failed.exception.cause))
            self.assertEqual(len(transport.calls), 3)
            self.assertEqual([event[0] for event in session.events], ["retire"])
            claim.release(owner=claim.lifecycle_owner)

    async def test_swallowed_report_failure_still_stops_next_phase_or_publication(self):
        for phase in ("prepare", "execute"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                store, acknowledged, claim = await live_attempt()
                transport = ConditionTransport()
                transport.release.set()
                transport.failure = ExchangeFailed("Authored private transport cause")
                session = SwallowingSession(store)
                # silent-defaults: allow-contract -- Disable only the other phase so this fixture reaches the selected phase failure.
                other_phase = "execute" if phase == "prepare" else "prepare"
                session.conditions[other_phase] = ()
                cleanup = RuntimeError("Authored private cleanup cause")
                session.retirement_failure = cleanup
                executor = live_executor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                )
                try:
                    with self.assertRaises(ExecutionFailed) as failed:
                        await executor.run(acknowledged)
                    self.assertIs(failed.exception.cause, session.caught)
                    self.assertIsInstance(failed.exception.cause, ApiFailure)
                    self.assertIs(failed.exception.api_failure, session.caught)
                    self.assertIn(cleanup, failed.exception.cleanup_failures)
                    # silent-defaults: allow-contract -- Only execution failure can retain the already-computed outcome.
                    self.assertIs(failed.exception.outcome, session.outcome if phase == "execute" else None)
                    (retained,) = store.records
                    self.assertIs(
                        type(retained),
                        EnteredCheckpoint if phase == "execute" else AcknowledgedCheckpoint,
                    )
                    if phase == "prepare":
                        self.assertNotIn("execute", [event[0] for event in session.events])
                finally:
                    session.retirement_failure = None
                    pass

    async def test_swallowed_report_cancellation_preserves_original_and_cleanup(self):
        class LaterFailureSession(SwallowingSession):
            async def prepare(self, acknowledged, instructions, *, report_condition):
                await super().prepare(acknowledged, instructions, report_condition=report_condition)
                raise later

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            transport = ConditionTransport()
            transport.release.set()
            previous = OSError("Authored private interrupted cause")
            interruption = asyncio.CancelledError()
            interruption.__cause__ = previous
            transport.failure = interruption
            later = RuntimeError("Authored later private binding cause")
            session = LaterFailureSession(store)
            cleanup = RuntimeError("Authored private cleanup cause")
            session.retirement_failure = cleanup
            executor = live_executor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            try:
                with self.assertRaises(asyncio.CancelledError) as cancelled:
                    await executor.run(acknowledged)
                self.assertIs(cancelled.exception, interruption)
                self.assertIs(executor.last_failure.cause, interruption)
                self.assertIs(executor.last_failure.interruption_cause, previous)
                self.assertIs(interruption.__context__, later)
                self.assertIn(cleanup, executor.last_failure.cleanup_failures)
                self.assertNotIn("execute", [event[0] for event in session.events])
            finally:
                session.retirement_failure = None
                pass

    async def test_later_binding_failure_does_not_replace_first_report_failure(self):
        class LaterFailureSession(SwallowingSession):
            async def prepare(self, acknowledged, instructions, *, report_condition):
                await super().prepare(acknowledged, instructions, report_condition=report_condition)
                raise later

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            transport = ConditionTransport()
            transport.release.set()
            transport.failure = ExchangeFailed("Authored private transport cause")
            later = RuntimeError("Authored later private binding cause")
            session = LaterFailureSession(store)
            executor = live_executor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            with self.assertRaises(ExecutionFailed) as failed:
                await executor.run(acknowledged)
            self.assertIs(failed.exception.cause, session.caught)
            self.assertIs(failed.exception.cause.__context__, later)
            self.assertIs(failed.exception.api_failure, session.caught)
            claim.release(owner=claim.lifecycle_owner)
