"""Execution follows verified source, preparation, progress and durable entry."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint, SuppressedCheckpoint, TerminalSelectedCheckpoint, EvidenceDigest
import tempfile
import unittest

from provider_core.analysis import AnalysisFailure
from provider_core.attempt_record import LocalExecutionPhase, ProtectedEvidence, TerminalOperation
from provider_core.execution import AttemptExecutor
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.api import api


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_observes_durable_entry_and_retains_outcome_before_return(self):
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            reply(progress_response(phase="running")),
            reply(observation()),
            reply(observation()),
        )
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            try:
                executor = AttemptExecutor(
                    api(transport, store.scope.api_origin), store, claim, session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
                )
                terminal = await executor.run(acknowledged)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertIs(type(session.events[1][1]), EnteredCheckpoint)
                self.assertEqual(await store.inventory(), (terminal,))
                self.assertIs(terminal.selection.operation, TerminalOperation.COMPLETE)
                self.assertEqual(terminal.selection.body_sha256, executor.delivery.items[0].body_sha256)
                self.assertIsNone(executor._outcome)
                self.assertEqual(len(transport.calls), 7)
                claim.release()
            finally:
                await store.close()

    async def test_classified_preparation_failure_never_enters_or_executes(self):
        transport = Transport(reply(observation()), reply(response()), reply(progress_response()), reply(observation()))
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.preparation = AnalysisFailure(
                "input_rejected", "Supply a geometry.", ProtectedEvidence("evidence.v1", b"reason")
            )
            try:
                terminal = await AttemptExecutor(
                    api(transport, store.scope.api_origin), store, claim, session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
                ).run(acknowledged)
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                self.assertIs(type(terminal.selection.attempt), AcknowledgedCheckpoint)
                self.assertIs(terminal.selection.operation, TerminalOperation.FAIL)
                self.assertEqual(len(transport.calls), 4)
                claim.release()
            finally:
                await store.close()
