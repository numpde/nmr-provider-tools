"""An observation deadline preserves the actual read and any completed science."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint, SuppressedCheckpoint, TerminalSelectedCheckpoint, EvidenceDigest
import asyncio
import errno
import json
import tempfile
import time
import unittest

from provider_core.attempt_record import SuppressionReason
from provider_core.execution import AttemptExecutor, ExecutionFailed, ExecutionRefused, ObservationGapExpired, ObservationRecovery
from provider_core.failure_projection import owned_details, owned_relationships
from provider_core.ownership import OwnershipRefused
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.conditions import ReportingSession
from tests.fakes.execution import PausedSession, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.api import api


class ObservationRecoveryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_success_does_not_gain_authority_before_the_timeout_callback_can_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = PausedSession(store, "execute")

            class LateReply(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    # Deliberately stall this test loop: the wait timer cannot
                    # run before the valid reply's coroutine returns late.
                    time.sleep(0.1)
                    return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

            transport = LateReply(reply(observation()))
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                observation_recovery=ObservationRecovery(
                    0.05,
                    0.02,
                    lambda _attempt, _failure, _seconds: self.fail("No retry"),
                    lambda _attempt, _failure: self.fail("Late reply is not admitted"),
                ),
            )
            try:
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIs(type(failed.exception.cause), ObservationGapExpired)
                self.assertTrue(failed.exception.cause.read_started)
                self.assertIsNone(failed.exception.cause.cause)
                self.assertEqual([item[0] for item in session.events], ["retire"])
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                await store.close()

    async def test_expiry_bounds_the_inflight_read_and_preserves_its_cause_and_completed_result(self):
        await self.check_expiry(delayed_read_cleanup=False)

    async def test_expiry_retires_science_before_original_read_cleanup_finishes(self):
        await self.check_expiry(delayed_read_cleanup=True)

    async def check_expiry(self, *, delayed_read_cleanup):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            retired = asyncio.Event()

            class RetiredSession(PausedSession):
                async def retire(self):
                    await super().retire()
                    retired.set()

            session = RetiredSession(store, "execute")
            entered = asyncio.Event()
            cleanup_release = asyncio.Event()
            interruptions = []

            class StalledRead(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    if len(self.calls) != 6:
                        return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                    self.calls.append((request, maximum_bytes))
                    self.pending_owner, self.retirement_confirmed = owner, False
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError as interruption:
                        interruptions.append(interruption)
                        if delayed_read_cleanup:
                            await cleanup_release.wait()
                        self.last_failure = ExchangeFailed("Authored interrupted read", primary=interruption)
                        raise

            transport = StalledRead(
                reply(observation()),
                reply(response()),
                reply(progress_response()),
                reply(observation()),
                reply(progress_response(phase="running")),
                reply(observation()),
            )
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                observation_recovery=ObservationRecovery(
                    0.1,
                    0.02,
                    lambda _attempt, _failure, _seconds: self.fail("No completed failure qualified for retry"),
                    lambda _attempt, _failure: self.fail("No read recovered"),
                ),
            )
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                session.release.set()
                await asyncio.wait_for(retired.wait(), 1)
                self.assertFalse(cleanup_release.is_set())
                with self.assertRaises(ExecutionFailed) as failed:
                    await asyncio.wait_for(task, 1)
                expired = failed.exception.cause
                self.assertIs(type(expired), ObservationGapExpired)
                self.assertTrue(expired.read_started)
                if delayed_read_cleanup:
                    self.assertIsNone(expired.cause)
                    self.assertTrue(expired.observation_pending)
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    with self.assertRaises(ExecutionRefused):
                        await executor.retain_suppressed_disposition()
                cleanup_release.set()
                with self.assertRaises(ExecutionFailed):
                    await executor.settle()
                self.assertIs(expired.cause, failed.exception.api_failure)
                self.assertIs(expired.cause.cause, transport.last_failure)
                self.assertIs(transport.last_failure.primary, interruptions[0])
                self.assertEqual(len(interruptions), 1)
                self.assertIs(failed.exception.outcome, session.outcome)
                self.assertTrue(executor.retirement_confirmed)
                self.assertEqual(len(transport.calls), 7)
                self.assertEqual(owned_details(expired)["gap_seconds"], 0.1)
                self.assertIn(("owned cause", expired.cause), tuple(owned_relationships(expired)))
                self.assertTrue(transport.retirement_confirmed)
                retained = await executor.retain_suppressed_disposition()
                self.assertIs(type(retained), SuppressedCheckpoint)
                self.assertIs(retained.reason, SuppressionReason.OBSERVATION_FAILED)
                self.assertFalse(hasattr(retained, "candidate"))
                self.assertIsNone(executor._outcome)
                self.assertEqual([item[0] for item in session.events], ["prepare", "execute", "retire"])
                claim.release()
            finally:
                session.release.set()
                cleanup_release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_lock_wait_expiry_has_no_invented_read_cause_after_an_earlier_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = ReportingSession(store)
            session.conditions = {"prepare": ("waiting_capacity",), "execute": ()}
            condition_sent = asyncio.Event()
            recovered = []

            class BlockedCondition(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    if request.method != "PUT" or json.loads(request.content)["condition_code"] is None:
                        return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                    self.calls.append((request, maximum_bytes))
                    self.pending_owner, self.retirement_confirmed = owner, False
                    condition_sent.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError as interruption:
                        self.last_failure = ExchangeFailed("Authored interrupted condition", primary=interruption)
                        raise

            transport = BlockedCondition(
                reply(b"maintenance", status=503), reply(observation()), reply(response()), reply(progress_response())
            )
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                observation_recovery=ObservationRecovery(
                    0.1,
                    0.02,
                    lambda _attempt, _failure, _seconds: None,
                    lambda _attempt, failure: recovered.append(failure),
                ),
            )
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(condition_sent.wait(), 1)
                with self.assertRaises(ExecutionFailed) as failed:
                    await asyncio.wait_for(task, 1)
                expired = failed.exception.cause
                self.assertIs(type(expired), ObservationGapExpired)
                self.assertFalse(expired.read_started)
                self.assertIsNone(expired.cause)
                self.assertIn("waiting for API exchange access", str(expired))
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].status, 503)
                self.assertIsNot(failed.exception.api_failure, recovered[0])
                self.assertEqual(failed.exception.api_failure.request_method, "PUT")
                self.assertTrue(failed.exception.api_failure.attempt_mutation_may_have_committed)
                self.assertEqual(len(transport.calls), 5)
                await executor.settle()
                claim.release()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await store.close()

    def test_policy_rejects_unbounded_windows_or_missing_protected_observers(self):
        def retry(_attempt, _failure, _seconds):
            pass

        def recovered(_attempt, _failure):
            pass

        for seconds in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                ObservationRecovery(seconds, 1, retry, recovered)
            with self.subTest(backoff=seconds), self.assertRaises(ValueError):
                ObservationRecovery(1, seconds, retry, recovered)
        for observers in ((None, recovered), (retry, None)):
            with self.subTest(observers=observers), self.assertRaises(ValueError):
                ObservationRecovery(1, 1, *observers)

    async def test_deadline_keeps_independent_cause_when_failed_read_retirement_is_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = PausedSession(store, "execute")
            cleanup_release = asyncio.Event()
            cleanup = OSError(errno.EIO, "PRIVATE retirement evidence")

            class InterruptedRetirement(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    received = await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                    self.pending_owner, self.retirement_confirmed = owner, False
                    return received

                async def retire(self, *, owner=None):
                    try:
                        await cleanup_release.wait()
                    except asyncio.CancelledError as interruption:
                        raise interruption from cleanup
                    await super().retire(owner=owner)

            transport = InterruptedRetirement(reply(b"maintenance", status=503))
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                observation_recovery=ObservationRecovery(
                    0.1,
                    0.02,
                    lambda _attempt, _failure, _seconds: self.fail("Retirement never completed"),
                    lambda _attempt, _failure: self.fail("No recovery"),
                ),
            )
            try:
                with self.assertRaises(ExecutionFailed) as failed:
                    await asyncio.wait_for(executor.run(acknowledged), 1)
                self.assertIs(type(failed.exception.cause), ObservationGapExpired)
                self.assertEqual(failed.exception.cause.cause.status, 503)
                self.assertTrue(any(cause.__cause__ is cleanup for cause in failed.exception.cleanup_failures))
                self.assertEqual([item[0] for item in session.events], ["retire"])
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                cleanup_release.set()
                await executor.settle()
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                cleanup_release.set()
                await store.close()
