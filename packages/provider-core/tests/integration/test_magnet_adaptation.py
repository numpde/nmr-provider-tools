"""Exercise a proposed Magnet adaptation through installed core runtime owners.

The local fixture models a required future preparation/prediction split. It does
not import, replace or qualify the current monolithic G01 processor or its models.
Pinned source paths and synthetic-result rationale belong to fakes/magnet.py.
"""

from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, DeliveryAcceptedCheckpoint
from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from tests.fakes.execution import LiveAttemptProbe, live_executor
import base64
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from provider_core.attempt_record import LocalExecutionPhase, StartPending
from provider_core.execution import ExecutionFailed, ExecutionRefused
from provider_core.ownership import AttemptClaims
from provider_core.starting import AttemptStarter
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import observation, progress_response, retained_start, start_response
from tests.fakes.job_input import facts, response
from tests.fakes.magnet import (
    ANALYSIS_KIND,
    RESULT_SCHEMA_ID,
    SOURCE,
    IndeterminatePrediction,
    MagnetSession,
    RetrySafeUnavailable,
    prediction,
)
from tests.fakes.api import api


class MagnetAdaptationTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_preserves_source_and_model_fields_and_publishes_opaque_product_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            pending, store, claim = await self._open(directory)
            interpreter = AsyncMock(return_value={"smiles": "  CCO  ", "solvent": "CDCl3"})
            worker = AsyncMock(return_value=prediction())
            session = MagnetSession(store, interpreter, worker)
            expected = {"schema_id": RESULT_SCHEMA_ID, "provider_attempt_key": "retained-start:01",
                        "model_input": {"smiles": "  CCO  ", "h_solvent": "CDCl3", "c_solvent": "CDCl3"},
                        "prediction": prediction()}
            expected_bytes = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            transport = Transport(reply(start_response(analysis_kind_ref=ANALYSIS_KIND)), *self._run_replies())
            client = api(transport, store.scope.api_origin)
            acknowledged = await AttemptStarter.live(client, claim, scope=store.scope).start(pending)
            executor = self._executor(client, store, claim, session)
            terminal = await executor.run(acknowledged)
            self.assertIs(type(terminal), DeliveryAcceptedCheckpoint)
            interpreter.assert_awaited_once_with(SOURCE)
            worker.assert_awaited_once_with({"smiles": "  CCO  ", "h_solvent": "CDCl3", "c_solvent": "CDCl3"})
            sent = json.loads(executor.delivery.items[0].body)
            content = base64.b64decode(sent["canonical_result_base64"])
            self.assertEqual(content, expected_bytes)
            self.assertEqual(json.loads(content), expected)
            self.assertEqual(json.loads(content)["prediction"]["future_binding_field"], {"retain": "exact product-owned content"})
            self.assertEqual(store.records, (terminal,))
            self.assertEqual(session.phases, [AcknowledgedCheckpoint, EnteredCheckpoint])
            self.assertTrue(session.retired)
            claim.release(owner=claim.lifecycle_owner)

    async def test_typed_preentry_unavailability_remains_retry_safe_without_a_new_core_outcome_type(self):
        with tempfile.TemporaryDirectory() as directory:
            pending, store, claim = await self._open(directory)
            unavailable = RetrySafeUnavailable("scripted interpreter unavailable before prediction")
            interpreter = AsyncMock(side_effect=unavailable)
            worker = AsyncMock()
            session = MagnetSession(store, interpreter, worker)
            transport = Transport(reply(start_response(analysis_kind_ref=ANALYSIS_KIND)), *self._run_replies(preparation_only=True))
            client = api(transport, store.scope.api_origin)
            acknowledged = await AttemptStarter.live(client, claim, scope=store.scope).start(pending)
            executor = self._executor(client, store, claim, session)
            with self.assertRaises(ExecutionFailed) as failed:
                await executor.run(acknowledged)
            self.assertIs(failed.exception.cause, unavailable)
            self.assertEqual(store.records, (AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission),))
            self.assertIs(acknowledged.local_phase, LocalExecutionPhase.NOT_ENTERED)
            worker.assert_not_awaited()
            self.assertTrue(session.retired)
            self.assertEqual(len(transport.calls), 4)
            self.assertTrue(executor.work_settled)
            with self.assertRaises(ExecutionRefused):
                await executor.settle()
            with self.assertRaises(ExecutionRefused):
                await executor.run(acknowledged)
            worker.assert_not_awaited()
            claim.release(owner=claim.lifecycle_owner)

    async def test_indeterminate_prediction_retains_live_owner_without_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            pending, store, claim = await self._open(directory)
            indeterminate = IndeterminatePrediction("scripted prediction reply lost")
            worker = AsyncMock(side_effect=indeterminate)
            session = MagnetSession(store, AsyncMock(return_value={"smiles": "  CCO  ", "solvent": "CDCl3"}), worker)
            client = api(Transport(reply(start_response(analysis_kind_ref=ANALYSIS_KIND)), *self._run_replies()), store.scope.api_origin)
            acknowledged = await AttemptStarter.live(client, claim, scope=store.scope).start(pending)
            executor = self._executor(client, store, claim, session)
            with self.assertRaises(ExecutionFailed) as failed:
                await executor.run(acknowledged)
            self.assertIs(failed.exception.cause, indeterminate)
            (entered,) = store.records
            self.assertIs(type(entered), EnteredCheckpoint)
            self.assertTrue(session.retired)
            self.assertTrue(executor.work_settled)
            with self.assertRaises(ExecutionRefused):
                await executor.settle()
            with self.assertRaises(ExecutionRefused):
                await executor.run(acknowledged)
            worker.assert_awaited_once()
            self.assertEqual(store.records, (entered,))
            claim.release(owner=claim.lifecycle_owner)

    async def _open(self, directory):
        pending = StartPending(scope(), ANALYSIS_KIND, facts(SOURCE), retained_start())
        store = LiveAttemptProbe()
        return pending, store, AttemptClaims.live().claim_live(pending, lifecycle_owner=store)

    def _executor(self, client, store, claim, session):
        return live_executor(client, store, claim, session,
                               delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.02, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)

    # silent-defaults: allow-contract -- This selects the documented synthetic preparation seam, without claiming real Magnet integration.
    def _run_replies(self, *, preparation_only=False):
        beginning = (reply(observation()), reply(response(SOURCE)), reply(progress_response()))
        if preparation_only:
            return beginning
        return (*beginning, reply(observation()), reply(progress_response(phase="running")),
                reply(observation()), reply(observation()))
