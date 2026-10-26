"""Live observation during entry persistence preserves fresh-read-before-execute."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint, SuppressedCheckpoint, TerminalSelectedCheckpoint, EvidenceDigest
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from provider_core.execution import AttemptExecutor
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.entry_write import EntryWriteBarrier
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.api import api


class EntryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_poll_then_completed_original_write_still_checks_state_before_one_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            polled = asyncio.Event()
            release_poll = asyncio.Event()

            class EntryTransport(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    if len(self.calls) == 5:
                        polled.set()
                        await release_poll.wait()
                    return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

            transport = EntryTransport(reply(observation()), reply(response()), reply(progress_response()),
                                       reply(observation()), reply(progress_response(phase="running")),
                                       reply(observation()), reply(observation()), reply(observation()))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                                       delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=0.01, task_settle_seconds=0.05)
            barrier = EntryWriteBarrier(store._database.mark_entered)
            task = None
            try:
                with patch.object(store._database, "mark_entered", side_effect=barrier.write):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                    await asyncio.wait_for(polled.wait(), 1)
                    self.assertEqual([event[0] for event in session.events], ["prepare"])
                    barrier.release.set()
                    release_poll.set()
                    terminal = await task
                self.assertIs(type(terminal), DeliveryAcceptedCheckpoint)
                self.assertEqual(await store.inventory(), (terminal,))
                self.assertEqual(barrier.calls, 1)
                self.assertEqual(len(transport.calls), 8)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                claim.release()
            finally:
                barrier.release.set()
                release_poll.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if store.pending_owner is executor:
                    await executor.settle()
                await store.close()
