"""Retry cannot turn unconfirmed/stale conditions into permission for effects."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock, patch
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint, SuppressedCheckpoint, TerminalSelectedCheckpoint, EvidenceDigest
import asyncio
import errno
import itertools
import tempfile
import unittest
from unittest.mock import Mock

from provider_core.api import ApiFailure
from provider_core.execution import (
    AttemptExecutor,
    ConditionRecovery,
    ConditionRecoveryExpired,
    ExecutionFailed,
    ExecutionRefused,
    ExecutionStopped,
    ObservationRecovery,
)
from provider_core.failure_projection import owned_details, owned_relationships
from provider_core.transport import ExchangeFailed
from tests.fakes.api import reply
from tests.fakes.attempts import observation
from tests.fakes.condition_recovery import ConditionOutageTransport
from tests.fakes.conditions import ConditionTransport, ReportingSession, SwallowingSession
from tests.fakes.execution import acknowledged_store
from tests.fakes.api import api


class ConditionRecoveryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_condition_evidence_survives_later_monitor_recovery(self):
        class InterruptedTransport(ConditionTransport):
            async def exchange(self, request, **kwargs):
                received = await super().exchange(request, **kwargs)
                if request.method == "GET" and self.failure is not None and self.reads > 1:
                    return reply(b"PRIVATE monitor unavailable", status=503)
                return received

        class WaitingSession(SwallowingSession):
            async def report(self, phase, report_condition):
                await super().report(phase, report_condition)
                cancelled_sends.append(executor.api.last_failure)
                await read_recovered.wait()

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = InterruptedTransport()
            transport.release.set()
            transport.failure = asyncio.CancelledError()
            transport.last_failure = ExchangeFailed("Interrupted condition delivery")
            cancelled_sends, read_recovered = [], asyncio.Event()

            def retry_read(_attempt, _failure, _seconds):
                transport.failure = None

            def recovered_read(_attempt, _failure):
                read_recovered.set()

            session = WaitingSession(store)
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(1, 0.02, Mock(), Mock()),
                observation_recovery=ObservationRecovery(1, 0.02, retry_read, recovered_read),
            )
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(executor.run(acknowledged), 2)
                self.assertTrue(read_recovered.is_set())
                self.assertEqual(len(cancelled_sends), 1)
                self.assertIs(executor.last_failure.latest_condition_failure, cancelled_sends[0])
                self.assertIs(cancelled_sends[0].cause, transport.last_failure)
                self.assertIsNone(executor.last_failure.api_failure)
                self.assertIsNone(executor.last_failure.condition_failure)
                self.assertNotIn("execute", [event[0] for event in session.events])
                claim.release()
            finally:
                await store.close()

    async def test_overlapping_call_invalidates_retry_without_resending_an_old_condition(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=2)
            transport.release_retirement.clear()
            session = ReportingSession(store)
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(1, 0.02, Mock(), Mock()),
            )
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(transport.retirement_entered.wait(), 1)
                with self.assertRaises(ExecutionRefused) as overlap:
                    await session.reporters["prepare"]("replacement_condition")
                transport.release_retirement.set()
                with self.assertRaises(ExecutionFailed) as caught:
                    await asyncio.wait_for(task, 2)
                self.assertIs(caught.exception.cause, overlap.exception)
                self.assertEqual(len(transport.condition_requests), 1)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                self.assertFalse(any(b"replacement_condition" in request.content for request, _ in transport.calls))
                claim.release()
            finally:
                transport.release_retirement.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_cancellation_after_monitor_recovery_retains_first_and_latest_condition_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=2)
            transport.faults = itertools.repeat(reply(b"PRIVATE condition unavailable", status=503))
            session, read_recovered = ReportingSession(store), asyncio.Event()
            failures = []

            def retry(_attempt, _phase, _condition, failure, _seconds):
                failures.append(failure)
                if len(failures) == 2:
                    transport.followup_observation = reply(b"PRIVATE monitor unavailable", status=503)

            def retry_read(_attempt, _failure, _seconds):
                # A distinct GET outage replaces the shared API failure. Its
                # subsequent successful read clears that active outage field.
                transport.followup_observation = None

            def recovered_read(_attempt, _failure):
                # Trigger interruption while this observer still holds API
                # access, before a queued condition can race another send.
                task.cancel()
                read_recovered.set()

            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(1, 0.02, retry, Mock()),
                observation_recovery=ObservationRecovery(1, 0.02, retry_read, recovered_read),
            )
            real_sleep = asyncio.sleep
            async def condition_backoff(seconds):
                # Keep the second condition wait pending while the independent
                # monitor recovers. This checks separate evidence owners, not
                # scheduler order between two legitimately paced API operations.
                if asyncio.current_task() is executor._task and len(failures) >= 2:
                    await asyncio.Event().wait()
                else:
                    await real_sleep(seconds)
            with patch("provider_core.execution.asyncio.sleep", new=condition_backoff):
                task = asyncio.create_task(executor.run(acknowledged))
                try:
                    await asyncio.wait_for(read_recovered.wait(), 1)
                    with self.assertRaises(asyncio.CancelledError) as interrupted:
                        await task
                    self.assertIs(interrupted.exception.__cause__, executor.last_failure)
                    self.assertGreaterEqual(len(failures), 2)
                    self.assertIs(executor.last_failure.condition_failure, failures[0])
                    self.assertIsNot(executor.last_failure.api_failure, failures[-1])
                    self.assertIn(("latest condition failure", failures[-1]), tuple(owned_relationships(executor.last_failure)))
                    self.assertEqual([event[0] for event in session.events], ["retire"])
                    claim.release()
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await store.close()

    async def test_retry_waiting_behind_the_monitor_cannot_dispatch_after_its_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=2)
            transport.block_read = 2
            transport.faults = itertools.repeat(reply(b"PRIVATE unavailable", status=503))
            session = ReportingSession(store)
            window = 0.04
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.005,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(window, 0.02, Mock(), Mock()),
            )
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(transport.read_blocked.wait(), 1)
                sends = len(transport.condition_requests)
                # The admission window includes this held real exchange lock.
                # Its read still has to settle; elapsed time cannot replace it.
                await asyncio.sleep(2 * window)
                transport.release_read.set()
                with self.assertRaises(ExecutionFailed) as caught:
                    await asyncio.wait_for(task, 2)
                self.assertIs(type(caught.exception.cause), ConditionRecoveryExpired)
                self.assertEqual(len(transport.condition_requests), sends)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                claim.release()
            finally:
                transport.release_read.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_actual_retirement_gates_resend_and_dependent_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=2)
            transport.release_retirement.clear()
            session, retry = ReportingSession(store), Mock()
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(1, 0.02, retry, Mock()),
            )
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(transport.retirement_entered.wait(), 1)
                self.assertEqual(len(transport.condition_requests), 1)
                self.assertEqual(session.events, [])
                retry.assert_not_called()
                transport.release_retirement.set()
                await asyncio.wait_for(task, 2)
                self.assertEqual(len(transport.condition_requests), 2)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                claim.release()
            finally:
                transport.release_retirement.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_existing_monitor_observes_stop_during_recovery_without_a_second_waiter(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=2)
            transport.faults = itertools.repeat(reply(b"PRIVATE unavailable", status=503))
            transport.followup_observation = reply(observation(job_state="cancelled"))
            session = ReportingSession(store)
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(1, 0.04, Mock(), Mock()),
            )
            try:
                with self.assertRaises(ExecutionFailed) as caught:
                    await asyncio.wait_for(executor.run(acknowledged), 2)
                self.assertIs(type(caught.exception.cause), ExecutionStopped)
                self.assertEqual(caught.exception.condition_failure.status, 503)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                claim.release()
            finally:
                await store.close()

    async def test_expiry_is_not_erased_by_a_binding_that_catches_it_and_returns_science(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=5)
            transport.faults = itertools.repeat(reply(b"PRIVATE unavailable", status=503))
            session = SwallowingSession(store)
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.005,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(0.03, 0.02, Mock(), Mock()),
            )
            try:
                with self.assertRaises(ExecutionFailed) as caught:
                    await asyncio.wait_for(executor.run(acknowledged), 2)
                self.assertIs(type(caught.exception.cause), ConditionRecoveryExpired)
                self.assertIs(caught.exception.cause, session.caught)
                self.assertIs(caught.exception.outcome, session.outcome)
                first = caught.exception.condition_failure
                self.assertIs(type(caught.exception.cause.cause), ApiFailure)
                self.assertIsNot(caught.exception.cause.cause, first)
                self.assertIn(("first condition failure", first), tuple(owned_relationships(caught.exception)))
                with self.assertRaises(ExecutionFailed):
                    await executor.settle()
                suppressed = await executor.retain_suppressed_disposition()
                self.assertIs(type(suppressed), SuppressedCheckpoint)
                self.assertFalse(hasattr(suppressed, "candidate"))
                self.assertIsNone(executor._outcome)
                claim.release()
            finally:
                await store.close()

    async def test_recovered_observer_failure_retains_first_fault_and_actual_received_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=2)
            fault = OSError(errno.EIO, "PRIVATE output error")
            session = ReportingSession(store)
            retry, recovered = Mock(), Mock(side_effect=fault)
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(1, 0.02, retry, recovered),
            )
            try:
                with self.assertRaises(ExecutionFailed) as caught:
                    await asyncio.wait_for(executor.run(acknowledged), 2)
                self.assertIs(caught.exception.cause, fault)
                self.assertIs(caught.exception.condition_failure, retry.call_args.args[3])
                self.assertEqual(caught.exception.condition_receipt.condition_code, "waiting_capacity")
                self.assertIs(owned_details(caught.exception)["condition_receipt_received"], True)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                claim.release()
            finally:
                await store.close()

    async def test_cleanup_and_unreviewed_failure_remain_operational_without_resend(self):
        for fault in (
            reply(b"PRIVATE permanent refusal", status=409),
            ExchangeFailed(
                "Independent cleanup failure", primary=TimeoutError(), cleanup_failures=(OSError(errno.EIO, "PRIVATE"),)
            ),
        ):
            with self.subTest(fault=type(fault).__name__), tempfile.TemporaryDirectory() as directory:
                store, acknowledged, claim = await acknowledged_store(directory)
                transport = ConditionOutageTransport(first_failed_put=2)
                transport.faults = iter((fault,))
                session, retry = ReportingSession(store), Mock()
                executor = AttemptExecutor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=0.01,
                    task_settle_seconds=0.1,
                    condition_recovery=ConditionRecovery(1, 0.02, retry, Mock()),
                )
                try:
                    with self.assertRaises(ExecutionFailed) as caught:
                        await executor.run(acknowledged)
                    self.assertIs(type(caught.exception.cause), ApiFailure)
                    retry.assert_not_called()
                    self.assertEqual(len(transport.condition_requests), 1)
                    self.assertEqual([event[0] for event in session.events], ["retire"])
                    claim.release()
                finally:
                    await store.close()
