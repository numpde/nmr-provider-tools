"""A bounded observation outage never repeats the original scientific operation."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint, SuppressedCheckpoint, TerminalSelectedCheckpoint, EvidenceDigest
import asyncio
import tempfile
import unittest

from provider_core.attempt_record import TerminalOperation
from provider_core.execution import AttemptExecutor, ObservationRecovery
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.execution import PausedSession, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.api import api


class CompletedDuringOutage(PausedSession):
    def __init__(self, store):
        super().__init__(store, "execute")
        self.produced = asyncio.Event()

    async def execute(self, prepared, *, report_condition):
        result = await super().execute(prepared, report_condition=report_condition)
        self.produced.set()
        return result


class ObservationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_science_waits_for_a_fresh_read_after_settled_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = CompletedDuringOutage(store)
            notices = []

            class OutageTransport(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    if len(self.calls) == 6:
                        await session.paused.wait()
                        session.release.set()
                        await session.produced.wait()
                    received = await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                    if len(self.calls) == 7:
                        self.retirement_confirmed = False
                        self.pending_owner = owner
                    return received

            transport = OutageTransport(
                reply(observation()),
                reply(response()),
                reply(progress_response()),
                reply(observation()),
                reply(progress_response(phase="running")),
                reply(observation()),
                reply(b"PRIVATE maintenance", status=503),
                reply(observation()),
                reply(observation()),
            )

            def retry(attempt, failure, seconds):
                self.assertIs(attempt.admission, acknowledged.admission)
                self.assertTrue(transport.retirement_confirmed)
                self.assertTrue(session.produced.is_set())
                self.assertEqual([item[0] for item in session.events], ["prepare", "execute"])
                notices.append(("retry", failure, seconds))

            def recovered(attempt, failure):
                self.assertIs(attempt.admission, acknowledged.admission)
                notices.append(("recovered", failure))

            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=0.01,
                task_settle_seconds=0.1,
                observation_recovery=ObservationRecovery(1, 0.02, retry, recovered),
            )
            try:
                terminal = await asyncio.wait_for(executor.run(acknowledged), 3)
                self.assertIs(terminal.selection.operation, TerminalOperation.COMPLETE)
                self.assertEqual(terminal.selection.body_sha256, executor.delivery.items[0].body_sha256)
                self.assertIsNone(executor._outcome)
                self.assertEqual(await store.inventory(), (terminal,))
                self.assertEqual([item[0] for item in notices], ["retry", "recovered"])
                self.assertIs(notices[0][1], notices[1][1])
                self.assertEqual(notices[0][1].status, 503)
                self.assertGreater(notices[0][2], 0)
                self.assertLessEqual(notices[0][2], 0.01)
                self.assertEqual(len(transport.calls), 9)
                self.assertEqual([item[0] for item in session.events], ["prepare", "execute", "retire"])
                claim.release()
            finally:
                session.release.set()
                await store.close()
