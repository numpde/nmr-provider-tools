"""Interrupted entry never invokes science; outcomes and cleanup remain owned."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint
from tests.fakes.execution import live_controller, live_executor
from tests.fakes.execution import LiveAttemptProbe
import asyncio
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from provider_core.execution import ExecutionFailed, ExecutionStopped
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.execution import Session, live_attempt
from tests.fakes.job_input import response
from tests.fakes.api import api


def normal_transport(*, last_observation=None):
    return Transport(
        reply(observation()),
        reply(response()),
        reply(progress_response()),
        reply(observation()),
        reply(progress_response(phase="running")),
        reply(last_observation or observation()),
        reply(observation()),
    )


class ExecutionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_settlement_preserves_the_same_cleanup_interruption_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            session = Session(store)
            previous = ExchangeFailed("Authored owned cleanup evidence", primary=OSError("PRIVATE cause"))
            interruption = asyncio.CancelledError()
            interruption.__cause__ = previous
            session.retirement_failure = interruption
            transport = Transport(reply(start_response(state="failed", replayed=True)))
            controller = live_controller(
                api(transport, store.scope.api_origin),
                store,
                AttemptClaims.live(),
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            with self.assertRaises(asyncio.CancelledError) as first:
                await controller.run(record())
            self.assertIs(first.exception, interruption)
            with self.assertRaises(asyncio.CancelledError) as second:
                await controller.settle()
            self.assertIs(second.exception, interruption)
            self.assertIs(interruption.__cause__, previous)
            self.assertEqual(len(transport.calls), 1)
            session.retirement_failure = None
            await controller.settle()

    async def test_late_cancelled_child_updates_the_same_escaped_failure_after_timed_out_join(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session = Session(store)
            entered, cancelled, release = (asyncio.Event() for _ in range(3))
            previous = OSError("PRIVATE prior model cause")
            originals = []

            # Keep the required named port while this patch isolates task cancellation.
            async def prepare(_acknowledged, _instructions, *, report_condition):  # noqa: ARG001
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as interruption:
                    originals.append(interruption)
                    cancelled.set()
                    await release.wait()
                    raise interruption from previous

            executor = live_executor(
                api(normal_transport(), store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.01,
            )
            try:
                with patch.object(session, "prepare", prepare):
                    task = asyncio.create_task(executor.run(acknowledged))
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    await asyncio.wait_for(cancelled.wait(), 1)
                    retained = executor.last_failure
                    self.assertIs(caught.exception.__cause__, retained)
                    self.assertIs(retained.cause, caught.exception)
                    self.assertIsNone(retained.operation_interruption)
                    self.assertIsInstance(retained.cleanup_failures[0], TimeoutError)
                    release.set()
                    await executor.settle()
                    self.assertIs(executor.last_failure, retained)
                    self.assertIs(retained.operation_interruption, originals[0])
                    self.assertIs(retained.operation_interruption.__cause__, previous)
                    self.assertIs(caught.exception.__cause__, retained)
                    self.assertEqual([event[0] for event in session.events], ["retire"])
                    claim.release(owner=claim.lifecycle_owner)
            finally:
                release.set()
                pass

    async def test_parent_cancellation_during_retirement_wait_is_recorded_once_with_original_child(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session = Session(store)
            entered, release = asyncio.Event(), asyncio.Event()
            async def retire():
                session.events.append(("retire",))
                entered.set()
                await release.wait()
            session.retire = retire
            transport = Transport(reply(observation()), ExchangeFailed("Authored input-response failure"))
            client = api(transport, store.scope.api_origin)
            executor = live_executor(client, store, claim, session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=1)
            task = asyncio.create_task(executor.run(acknowledged))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                original_retirement = executor._retirement_task
                task.cancel()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                retained = executor.last_failure
                self.assertIs(retained.cause, client.last_failure)
                self.assertEqual(retained.cleanup_failures, (caught.exception,))
                self.assertIs(executor._retirement_task, original_retirement)
                self.assertFalse(original_retirement.done())
                self.assertEqual([event[0] for event in session.events], ["retire"])
                with self.assertRaises(OwnershipRefused):
                    claim.release(owner=claim.lifecycle_owner)
                release.set()
                await executor.settle()
                self.assertIs(executor.last_failure, retained)
                self.assertEqual(retained.cleanup_failures, (caught.exception,))
                self.assertEqual([event[0] for event in session.events], ["retire"])
                self.assertEqual(len(transport.calls), 2)
                claim.release(owner=claim.lifecycle_owner)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                pass

    async def test_api_primary_and_cleanup_cancellation_keep_both_exact_causes(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session = Session(store)
            previous = OSError("PRIVATE prior cleanup cause")
            cleanup = asyncio.CancelledError()
            cleanup.__cause__ = previous
            session.retirement_failure = cleanup
            transport = Transport(reply(observation()), ExchangeFailed("Authored input-response failure"))
            client = api(transport, store.scope.api_origin)
            executor = live_executor(
                client, store, claim, session, delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            with self.assertRaises(asyncio.CancelledError) as caught:
                await executor.run(acknowledged)
            self.assertIs(caught.exception, cleanup)
            retained = caught.exception.__cause__
            self.assertIs(retained, executor.last_failure)
            self.assertIs(retained.cause, client.last_failure)
            self.assertIs(retained.api_failure, client.last_failure)
            self.assertIs(retained.interruption_cause, previous)
            self.assertEqual(retained.cleanup_failures, (cleanup,))
            self.assertEqual(len(transport.calls), 2)
            session.retirement_failure = None
            await executor.settle()
            self.assertIs(executor.last_failure, retained)
            self.assertEqual([event[0] for event in session.events], ["retire", "retire"])
            claim.release(owner=claim.lifecycle_owner)

    async def test_unconfirmed_running_progress_prevents_entry_and_execution(self):
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            ExchangeFailed("running reply lost"),
        )
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session = Session(store)
            executor = live_executor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            with self.assertRaises(ExecutionFailed):
                await executor.run(acknowledged)
            self.assertEqual(store.records, (AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission),))
            self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
            self.assertTrue(executor.last_failure.api_failure.attempt_mutation_may_have_committed)
            claim.release(owner=claim.lifecycle_owner)

    async def test_unencodable_result_is_preserved_and_retired_without_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session = Session(store)
            session.outcome = replace(session.outcome, content=b"x" * 786433)
            executor = live_executor(
                api(normal_transport(), store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            with self.assertRaises(ExecutionFailed) as failed:
                await executor.run(acknowledged)
            self.assertIs(failed.exception.outcome, session.outcome)
            self.assertIsInstance(failed.exception.cause, ValueError)
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            with self.assertRaises(OwnershipRefused):
                claim.release(owner=claim.lifecycle_owner)
            with self.assertRaises(ExecutionFailed):
                await executor.settle()
            (retained,) = store.records
            self.assertIs(type(retained), EnteredCheckpoint)


    async def test_closed_job_before_execution_prevents_science_and_publication(self):
        store, acknowledged, claim = await live_attempt()
        session = Session(store)
        executor = live_executor(
            api(normal_transport(last_observation=observation(job_state="closed")), store.scope.api_origin),
            store, claim, session, delivery=RecordingDelivery(), on_delivery_retry=Mock(),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1,
        )
        with self.assertRaises(ExecutionFailed) as failed:
            await executor.run(acknowledged)
        self.assertIsInstance(failed.exception.cause, ExecutionStopped)
        self.assertIs(type(store.records[0]), EnteredCheckpoint)
        self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
        self.assertEqual([], executor.delivery.items)
        claim.release(owner=claim.lifecycle_owner)

    async def test_failed_retirement_preserves_approved_delivery_and_requires_cleanup_before_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session, delivery = Session(store), RecordingDelivery()
            cleanup = RuntimeError("private retirement cause")
            session.retirement_failure = cleanup
            executor = live_executor(api(normal_transport(), store.scope.api_origin), store, claim, session,
                delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            with self.assertRaises(ExecutionFailed) as failed:
                await executor.run(acknowledged)
            self.assertIs(failed.exception.cause, cleanup)
            self.assertIsNone(failed.exception.outcome)
            self.assertIsNotNone(failed.exception.delivery_acceptance)
            self.assertIsNone(executor._delivery_outcome)
            self.assertNotIn("private retirement cause", str(failed.exception))
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            (retained,) = store.records
            self.assertIs(type(retained), DeliveryAcceptedCheckpoint)
            self.assertEqual(len(delivery.items), 1)
            self.assertEqual(retained.selection.body_sha256, delivery.items[0].body_sha256)
            with self.assertRaises(OwnershipRefused):
                claim.release(owner=claim.lifecycle_owner)
            session.retirement_failure = None
            self.assertEqual(await executor.settle(), retained)
            self.assertEqual(len(delivery.items), 1)
            claim.release(owner=claim.lifecycle_owner)
