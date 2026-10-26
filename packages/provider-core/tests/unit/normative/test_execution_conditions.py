"""Condition acknowledgement precedes dependent binding effects in each phase."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint, SuppressedCheckpoint, TerminalSelectedCheckpoint, EvidenceDigest
import asyncio
import json
import tempfile
import unittest

from provider_core.attempt_record import LocalExecutionPhase, TerminalOperation
from provider_core.execution import AttemptExecutor
from tests.fakes.conditions import ConditionTransport, ReportingSession
from tests.fakes.execution import acknowledged_store
from tests.fakes.api import api


class ObservedExecutor(AttemptExecutor):
    async def _observe_active(self):
        if self.condition_transport.blocked.is_set() and not self.condition_transport.release.is_set():
            self.poll_attempted.set()
        await super()._observe_active()


class ExecutionConditionTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_and_clear_in_each_phase_preserve_running_entry_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionTransport()
            transport.release.set()
            session = ReportingSession(store)
            try:
                terminal = await AttemptExecutor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                ).run(acknowledged)
                commands = [json.loads(request.content) for request, _ in transport.calls if request.method == "PUT"]
                self.assertEqual(
                    [(command["phase"], command["condition_code"]) for command in commands],
                    [
                        ("preparing", None),
                        ("preparing", "waiting_capacity"),
                        ("preparing", None),
                        ("running", None),
                        ("running", "waiting_capacity"),
                        ("running", None),
                    ],
                )
                self.assertIs(type(session.events[0][1]), AcknowledgedCheckpoint)
                self.assertIs(type(session.events[1][1]), EnteredCheckpoint)
                self.assertIs(terminal.selection.operation, TerminalOperation.COMPLETE)
                claim.release()
            finally:
                await store.close()

    async def test_pending_condition_blocks_effect_and_serializes_polling(self):
        for phase in ("prepare", "execute"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                store, acknowledged, claim = await acknowledged_store(directory)
                transport = ConditionTransport()
                session = ReportingSession(store)
                # silent-defaults: allow-contract -- Disable only the other phase so this fixture reaches the selected phase failure.
                other_phase = "execute" if phase == "prepare" else "prepare"
                session.conditions[other_phase] = ()
                executor = ObservedExecutor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                    delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=0.005,
                    task_settle_seconds=0.1,
                )
                executor.condition_transport = transport
                executor.poll_attempted = asyncio.Event()
                task = asyncio.create_task(executor.run(acknowledged))
                try:
                    await asyncio.wait_for(transport.blocked.wait(), 1)
                    reads = transport.reads
                    await asyncio.wait_for(executor.poll_attempted.wait(), 1)
                    self.assertEqual(transport.maximum_in_flight, 1)
                    self.assertEqual(transport.reads, reads)
                    self.assertNotIn(phase, [event[0] for event in session.events])
                    transport.release.set()
                    await asyncio.wait_for(task, 1)
                    self.assertGreater(transport.reads, reads)
                    claim.release()
                finally:
                    transport.release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await store.close()
