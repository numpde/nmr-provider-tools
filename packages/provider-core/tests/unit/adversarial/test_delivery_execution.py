"""Late suppression commits settle without retaining or publishing the candidate."""

import asyncio
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from provider_core.analysis import AnalysisFailure
from provider_core.attempt_record import ProtectedEvidence, SuppressionReason
from provider_core.checkpoint_store import CheckpointRefused
from provider_core.execution import AttemptExecutor, ExecutionFailed
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, SuppressedCheckpoint
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.checkpoint_execution import CheckpointSession, RecordingDelivery, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.api import api


class InterruptedSuppressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_entry_persistence_prevents_scientific_execution_and_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = CheckpointSession(store), RecordingDelivery()
            transport = Transport(reply(observation()), reply(response()), reply(progress_response()),
                                  reply(observation()), reply(progress_response(phase="running")))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01, task_settle_seconds=1)
            try:
                with patch.object(store._database, "mark_entered", side_effect=OSError("entry persistence failed")):
                    with self.assertRaises(ExecutionFailed):
                        await executor.run(acknowledged)
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                self.assertEqual(delivery.items, [])
                self.assertIs(type((await store.inventory())[0]), AcknowledgedCheckpoint)
                self.assertEqual(len(transport.calls), 5)
            finally:
                await store.close()

    async def test_cancelled_suppression_wait_settles_original_disposition_and_releases_body(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session, delivery = CheckpointSession(store), RecordingDelivery()
            session.preparation = AnalysisFailure("input_rejected", "Correct geometry units.",
                                                  ProtectedEvidence("evidence.v1", b"PRIVATE_SCIENTIFIC_REASON"))
            transport = Transport(reply(observation()), reply(response()), reply(progress_response()),
                                  reply(observation(state="expired")))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01, task_settle_seconds=1)
            entered, release = threading.Event(), threading.Event()
            original = store._database.suppress

            def delayed(*args):
                entered.set()
                release.wait(3)
                return original(*args)

            try:
                with self.assertRaises(ExecutionFailed):
                    await executor.run(acknowledged)
                with patch.object(store._database, "suppress", side_effect=CheckpointRefused("Suppression refused before mutation.")):
                    with self.assertRaises(ExecutionFailed):
                        await executor.retain_suppressed_disposition()
                self.assertIsNone(executor._unconfirmed)
                self.assertIsNotNone(executor._outcome)
                with patch.object(store._database, "suppress", side_effect=delayed) as write:
                    task = asyncio.create_task(executor.retain_suppressed_disposition())
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        release.set()
                    await executor.settle()
                    self.assertEqual(write.call_count, 1)
                (suppressed,) = await store.inventory()
                self.assertIs(type(suppressed), SuppressedCheckpoint)
                self.assertIs(suppressed.reason, SuppressionReason.SERVER_STOP)
                self.assertIsNone(executor._outcome)
                self.assertIsNone(executor.last_failure.outcome)
                self.assertEqual(delivery.items, [])
                claim.release()
            finally:
                release.set()
                if store.pending_operation is not None:
                    await store.settle(owner=store.pending_owner)
                await store.close()
