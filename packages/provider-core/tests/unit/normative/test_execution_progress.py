"""Unconfirmed phase delivery needs a fresh lifecycle read, never a PUT replay."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from provider_core.producer_checkpoint import EnteredCheckpoint
from tests.fakes.execution import live_executor
import json
import tempfile
import unittest

from provider_core.attempt_record import TerminalOperation
from tests.fakes.execution import Session, live_attempt
from tests.fakes.phase_progress import PhaseProgressTransport
from tests.fakes.api import api


class PhaseProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_phase_can_continue_after_retirement_notice_and_fresh_allowed_read(self):
        for phase in ("preparing", "running"):
            with self.subTest(phase=phase):
                await self.check_phase(phase)

    async def check_phase(self, phase):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await live_attempt()
            session = Session(store)
            transport = PhaseProgressTransport(phase)
            notices = []

            def unconfirmed(attempt, reported_phase, failure):
                self.assertIs(attempt, acknowledged)
                self.assertEqual(reported_phase, phase)
                self.assertIs(failure, client.last_failure)
                self.assertEqual(failure.status, 503)
                self.assertTrue(failure.attempt_mutation_may_have_committed)
                self.assertTrue(transport.retirement_confirmed)
                self.assertEqual(transport.fresh_reads, 0)
                # silent-defaults: allow-contract -- Only phases reached before the injected stop belong in the expected event order.
                expected_events = [] if phase == "preparing" else ["prepare"]
                self.assertEqual([event[0] for event in session.events], expected_events)
                notices.append(failure)

            client = api(transport, store.scope.api_origin)
            executor = live_executor(
                client,
                store,
                claim,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                on_unconfirmed_phase_progress=unconfirmed,
            )
            terminal = await executor.run(acknowledged)
            self.assertEqual(len(notices), 1)
            self.assertEqual(transport.fresh_reads, 1)
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            self.assertIs(type(session.events[1][1]), EnteredCheckpoint)
            self.assertIs(terminal.selection.operation, TerminalOperation.COMPLETE)
            self.assertEqual(terminal.selection.body_sha256, executor.delivery.items[0].body_sha256)
            self.assertIsNone(executor._outcome)
            self.assertEqual(store.records, (terminal,))
            commands = [json.loads(request.content) for request, _ in transport.calls if request.method == "PUT"]
            self.assertEqual(
                [(item["phase"], item["condition_code"]) for item in commands], [("preparing", None), ("running", None)]
            )
            self.assertIsNone(executor.last_failure)
            claim.release(owner=claim.lifecycle_owner)

    async def test_invalid_observer_refuses_before_session_or_api_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _acknowledged, claim = await live_attempt()
            transport = PhaseProgressTransport("preparing")
            session = Session(store)
            with self.assertRaises(ValueError):
                live_executor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                    on_unconfirmed_phase_progress=False,
                )
            self.assertEqual(transport.calls, [])
            self.assertEqual(session.events, [])
            claim.release(owner=claim.lifecycle_owner)
