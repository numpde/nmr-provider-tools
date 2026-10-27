"""Proved native crashes report one exact failure without a final API read."""

import asyncio
import json
import unittest
from unittest.mock import Mock

from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.attempt_record import TerminalOperation
from provider_core.job_input import verify_job_input
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.failure_projection import owned_details, owned_relationships
from provider_core.execution import ObservationRecovery
from provider_core.delivery_store import DeliveryFull
from provider_core.analysis import AnalysisFailure, AnalysisSuccess
from provider_core.attempt_record import ProtectedEvidence
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.crashing_analysis import CrashingSession
from tests.fakes.job_input import response
from tests.fakes.runner import RunningSupervisor


class ObservedCrashTests(unittest.IsolatedAsyncioTestCase):
    def session(self, phase):
        supervisor = RunningSupervisor()
        self.addCleanup(supervisor.close)
        native = supervisor.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(native.close)
        native.connect()
        return CrashingSession(native, phase)

    def transport(self, phase):
        replies = [reply(start_response()), reply(observation()), reply(progress_response())]
        if phase == "running":
            replies.extend([reply(observation()), reply(progress_response(phase="running")), reply(observation())])
        replies.append(reply(b"API unavailable after crash", status=503))
        return Transport(*replies)

    def controller(self, session, transport, *, observation_recovery=None):
        return AttemptController.live(
            api(transport, scope().api_origin), AttemptClaims.live(), session,
            scope=scope(), stopping=asyncio.Event(),
            start_policy=LiveStartPolicy(cadence_seconds=1, on_retry=Mock(), on_recovered=Mock()),
            preverified_input=verify_job_input(response(), feed=record().input),
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            poll_seconds=1, task_settle_seconds=1,
            observation_recovery=observation_recovery,
        )

    async def test_each_active_phase_reports_crash_once_without_reading_the_outage(self):
        for phase, calls in (("preparing", 3), ("running", 6)):
            with self.subTest(phase=phase):
                session, transport = self.session(phase), self.transport(phase)
                controller = self.controller(session, transport)
                accepted = await controller.run(record())
                self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
                self.assertEqual(calls, len(transport.calls))
                self.assertEqual(1, len(controller.delivery.items))
                item = controller.delivery.items[0]
                self.assertIs(TerminalOperation.FAIL, item.operation)
                body = json.loads(item.body)
                self.assertEqual("worker_crashed", body["failure_code"])
                self.assertIn("9", body["failure_message"])
                self.assertNotIn("PRIVATE", body["failure_message"])
                self.assertLessEqual(len(body["failure_message"]), 1024)
                self.assertTrue(controller.shutdown_ready)
                crash = controller.operation.observed_crash
                self.assertEqual(-9, owned_details(crash)["returncode"])
                self.assertIn(("owned cause", crash.cause), list(owned_relationships(crash)))

    async def test_wrong_phase_owner_or_recreated_admission_cannot_publish_a_crash(self):
        for mismatch in ("phase", "owner", "admission", "interruption"):
            with self.subTest(mismatch=mismatch):
                session = self.session("preparing")
                if mismatch == "phase":
                    session.claimed_phase = "running"
                elif mismatch == "owner":
                    session.claimed_owner = object()
                elif mismatch == "admission":
                    session.clone_acknowledgement = True
                else:
                    session.interrupt_first = True
                controller = self.controller(session, self.transport("preparing"))
                with self.assertRaises(ControllerFailed):
                    await controller.run(record())
                self.assertEqual([], controller.delivery.items)
                self.assertNotIn("execute", session.events)

    async def test_crash_admitted_before_pending_monitor_read_fails_still_reaches_delivery(self):
        session = self.session("preparing")
        session.begin_crash.clear()

        class OutageDuringCrash(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if len(self.calls) == 3:
                    session.begin_crash.set()
                    await asyncio.wait_for(session.crash_ready.wait(), 2)
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        original = self.transport("preparing")
        controller = self.controller(session, OutageDuringCrash(*original.replies))
        controller._poll_seconds = 0.01
        accepted = await controller.run(record())
        self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
        self.assertEqual("worker_crashed", json.loads(controller.delivery.items[0].body)["failure_code"])

    async def test_recovery_logging_failure_cannot_veto_an_already_admitted_crash(self):
        session = self.session("preparing")
        session.begin_crash.clear()

        class OutageDuringCrash(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if len(self.calls) == 3:
                    session.begin_crash.set()
                    await asyncio.wait_for(session.crash_ready.wait(), 2)
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        recovery = ObservationRecovery(
            gap_seconds=0.2, backoff_max_seconds=0.04,
            on_retry=Mock(side_effect=OSError("Logging sink failed")), on_recovered=Mock(),
        )
        controller = self.controller(session, OutageDuringCrash(*self.transport("preparing").replies),
                                     observation_recovery=recovery)
        controller._poll_seconds = 0.01
        accepted = await controller.run(record())
        self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
        recovery.on_retry.assert_not_called()
        self.assertEqual(1, len(controller.delivery.items))

    async def test_delivery_logging_failure_cannot_cancel_the_selected_exact_crash_report(self):
        class FullOnce(RecordingDelivery):
            def __init__(self):
                super().__init__()
                self.attempted = []

            async def submit(self, item):
                self.attempted.append(item.body)
                if len(self.attempted) == 1:
                    raise DeliveryFull("Relay is full")
                return await super().submit(item)

        delivery, diagnostic = FullOnce(), OSError("Retry logging sink failed")
        session = self.session("preparing")
        controller = self.controller(session, self.transport("preparing"))
        controller.delivery = delivery
        controller._on_delivery_retry = Mock(side_effect=diagnostic)
        accepted = await controller.run(record())
        self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
        self.assertEqual(2, len(delivery.attempted))
        self.assertIs(delivery.attempted[0], delivery.attempted[1])
        self.assertIs(diagnostic, controller.operation.delivery_diagnostic_failure)
        self.assertEqual(1, len(delivery.items))

    async def test_native_cleanup_crash_preserves_already_chosen_complete_or_fail(self):
        evidence = ProtectedEvidence("fixture.outcome.v1", b"original scientific outcome")
        for outcome, expected in (
            (AnalysisSuccess("result.v1", b"result", evidence), TerminalOperation.COMPLETE),
            (AnalysisFailure("input_rejected", "Input was rejected", evidence), TerminalOperation.FAIL),
        ):
            with self.subTest(operation=expected):
                delivery = RecordingDelivery()

                class CleanupCrash(CrashingSession):
                    async def execute(self, prepared, *, report_condition):
                        self.events.append("execute")
                        if prepared is not self.handle:
                            raise AssertionError("Foreign scientific handle")
                        return outcome

                    async def retire(self):
                        await asyncio.wait_for(delivery.accepted.wait(), 1)
                        await self._crash()

                native = self.session("running").native
                session = CleanupCrash(native, "running")
                replies = list(self.transport("running").replies)
                replies[-1] = reply(observation())
                controller = self.controller(session, Transport(*replies))
                controller.delivery = delivery
                with self.assertRaises(ControllerFailed):
                    await controller.run(record())
                self.assertEqual(1, len(delivery.items))
                self.assertIs(expected, delivery.items[0].operation)
                if expected is TerminalOperation.FAIL:
                    self.assertEqual("input_rejected", json.loads(delivery.items[0].body)["failure_code"])
                self.assertIsNone(controller.operation.observed_crash)
