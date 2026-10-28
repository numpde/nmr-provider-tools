"""Retaining interrupted outcomes never repeats science or releases uncertain work."""

from tests.fakes.execution import live_executor
import asyncio
import tempfile
import unittest

from provider_core.analysis import AnalysisFailure
from provider_core.attempt_record import ProtectedEvidence, SuppressionReason
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, encode_checkpoint
from provider_core.execution import ExecutionFailed, ExecutionStopped
from provider_core.ownership import OwnershipRefused
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.execution import Session, live_attempt
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.job_input import response
from tests.fakes.api import api


def unexpected_delivery_retry(*_args):
    raise AssertionError("Suppressed outcome cannot be submitted")


async def stopped_executor(directory, retirement_failure):
    store, acknowledged, claim = await live_attempt()
    session = Session(store)
    session.retirement_failure = retirement_failure
    transport = Transport(reply(observation()), reply(response()), reply(progress_response()), reply(observation()),
                          reply(progress_response(phase="running")), reply(observation()), reply(observation(job_state="closed")))
    executor = live_executor(api(transport, store.scope.api_origin), store, claim, session,
                               delivery=RecordingDelivery(), on_delivery_retry=unexpected_delivery_retry,
                               delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
                               allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
    try:
        await executor.run(acknowledged)
    except ExecutionFailed as failure:
        if not isinstance(failure.cause, ExecutionStopped):
            raise
    else:
        raise AssertionError("Fixture did not stop the completed result's acceptance")
    return store, executor, session, claim, transport


class SuppressionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_caller_cancellation_preserves_returned_result_after_original_api_settles(self):
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

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session = Session(store)
            transport = PendingTransport(reply(observation()), reply(response()), reply(progress_response()), reply(observation()),
                                         reply(progress_response(phase="running")), reply(observation()))
            executor = live_executor(api(transport, store.scope.api_origin), store, claim, session,
                               delivery=RecordingDelivery(), on_delivery_retry=unexpected_delivery_retry,
                               delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
                                       allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            task = asyncio.create_task(executor.run(acknowledged))
            await asyncio.wait_for(reading.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ExecutionFailed) as unretained:
                await executor.settle()
            self.assertIn("suppression disposition", str(unretained.exception.cleanup_failures[-1]))
            self.assertIs(unretained.exception.outcome, session.outcome)
            self.assertIsNone(transport.pending_owner)
            self.assertTrue(transport.retirement_confirmed)
            retained = await executor.retain_suppressed_disposition()
            self.assertIs(retained.reason, SuppressionReason.CALLER_CANCELLED)
            self.assertIsInstance(executor.last_failure.cause, asyncio.CancelledError)
            self.assertIsNone(executor.last_failure.outcome)
            self.assertNotIn(session.outcome.content, encode_checkpoint(retained))
            self.assertNotIn(session.outcome.evidence.content, encode_checkpoint(retained))
            self.assertEqual(executor.delivery.items, [])
            self.assertEqual(len(transport.calls), 7)
            claim.release(owner=claim.lifecycle_owner)

    async def test_classified_preparation_failure_can_be_retained_after_stop_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session = Session(store)
            session.preparation = AnalysisFailure("invalid_input", "Supply coordinates.", ProtectedEvidence("evidence.v1", b"reason"))
            transport = Transport(reply(observation()), reply(response()), reply(progress_response()),
                                  reply(observation(job_state="closed")))
            executor = live_executor(api(transport, store.scope.api_origin), store, claim, session,
                               delivery=RecordingDelivery(), on_delivery_retry=unexpected_delivery_retry,
                               delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
                                       allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            with self.assertRaises(ExecutionFailed):
                await executor.run(acknowledged)
            retained = await executor.retain_suppressed_disposition()
            self.assertIs(type(retained.attempt), AcknowledgedCheckpoint)
            self.assertIs(retained.reason, SuppressionReason.SERVER_STOP)
            self.assertIsNone(executor.last_failure.outcome)
            self.assertFalse(hasattr(retained, "candidate"))
            self.assertEqual(executor.delivery.items, [])
            self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
            self.assertEqual(len(transport.calls), 4)
            claim.release(owner=claim.lifecycle_owner)


    async def test_durable_suppression_cannot_release_unconfirmed_worker_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            store, executor, session, claim, transport = await stopped_executor(directory, RuntimeError("private cleanup failure"))
            retained = await executor.retain_suppressed_disposition()
            self.assertEqual(store.records, (retained,))
            with self.assertRaises(OwnershipRefused):
                claim.release(owner=claim.lifecycle_owner)
            session.retirement_failure = None
            self.assertIsNone(await executor.settle())
            self.assertEqual(len(transport.calls), 7)
            self.assertEqual([event[0] for event in session.events].count("execute"), 1)
            claim.release(owner=claim.lifecycle_owner)
