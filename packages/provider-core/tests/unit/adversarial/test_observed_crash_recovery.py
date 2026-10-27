"""Real native exit evidence cannot erase stop, deadline or I/O ownership.

API replies and the observation clock are controlled here. The child exit,
original runner session and crash admission use the installed SDK.
"""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.execution import ExecutionStopped, ObservationGapExpired, ObservationRecovery
from provider_core.job_input import verify_job_input
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.crashing_analysis import CrashingSession
from tests.fakes.job_input import response
from tests.fakes.runner import RunningSupervisor


class ObservedCrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        supervisor = RunningSupervisor()
        self.addCleanup(supervisor.close)
        native = supervisor.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(native.close)
        native.connect()
        session = CrashingSession(native, "preparing")
        session.begin_crash.clear()
        return session

    def controller(self, session, transport, recovery, *, poll_seconds):
        return AttemptController.live(
            api(transport, scope().api_origin), AttemptClaims.live(), session,
            scope=scope(), stopping=asyncio.Event(),
            start_policy=LiveStartPolicy(cadence_seconds=1, on_retry=Mock(), on_recovered=Mock()),
            preverified_input=verify_job_input(response(), feed=record().input),
            delivery=RecordingDelivery(), on_delivery_retry=Mock(),
            allowed_job_states=frozenset({"open"}),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            poll_seconds=poll_seconds, task_settle_seconds=1, observation_recovery=recovery,
        )

    def initial_replies(self):
        return (reply(start_response()), reply(observation()), reply(progress_response()))

    def clock(self, clock):
        # Patch only execution's clock lookup. Native I/O and asyncio's own
        # scheduling still use their real event loop and monotonic deadlines.
        namespace = SimpleNamespace(**vars(asyncio))
        namespace.get_running_loop = lambda: SimpleNamespace(time=lambda: clock.now)
        return patch("provider_core.execution.asyncio", namespace)

    async def test_received_stop_still_vetoes_crash_reporting_at_observation_deadline(self):
        session = self.session()
        clock = SimpleNamespace(now=asyncio.get_running_loop().time())
        gap = 1

        class StopAtDeadline(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if len(self.calls) == 3:
                    session.begin_crash.set()
                    await asyncio.wait_for(session.crash_ready.wait(), 2)
                    clock.now += gap
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        transport = StopAtDeadline(*self.initial_replies(), reply(observation(job_state="cancelled")))
        recovery = ObservationRecovery(gap, 0.1, Mock(), Mock())
        controller = self.controller(session, transport, recovery, poll_seconds=0.01)
        with self.clock(clock), self.assertRaises(ControllerFailed):
            await asyncio.wait_for(controller.run(record()), 3)
        self.assertIsNotNone(session.native.original_exit)
        self.assertIsInstance(controller.operation.last_failure.cause, ExecutionStopped)
        self.assertEqual([], controller.delivery.items)
        self.assertNotIn("execute", session.events)
        self.assertEqual(4, len(transport.calls))

    async def test_crash_during_approved_recovery_wait_wakes_delivery_without_another_read(self):
        session = self.session()
        retry = Mock(side_effect=lambda *_: session.begin_crash.set())
        recovered = Mock()
        transport = Transport(*self.initial_replies(), reply(b"maintenance", status=503))
        recovery = ObservationRecovery(3, 0.5, retry, recovered)
        controller = self.controller(session, transport, recovery, poll_seconds=0.5)
        with patch("provider_core.execution.random.uniform", return_value=0.5):
            accepted = await asyncio.wait_for(controller.run(record()), 4)
        self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
        retry.assert_called_once()
        recovered.assert_not_called()
        self.assertEqual(4, len(transport.calls))
        self.assertEqual(1, len(controller.delivery.items))
        self.assertEqual("worker_crashed", json.loads(controller.delivery.items[0].body)["failure_code"])
        self.assertTrue(controller.shutdown_ready)
        self.assertNotIn("execute", session.events)

    async def test_original_exit_observed_after_gap_expiry_cannot_become_a_failure_report(self):
        session = self.session()
        clock = SimpleNamespace(now=asyncio.get_running_loop().time())
        gap = 1

        class CrashAfterDeadline(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if len(self.calls) == 3:
                    clock.now += gap
                    session.begin_crash.set()
                    await asyncio.wait_for(session.crash_ready.wait(), 2)
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        transport = CrashAfterDeadline(*self.initial_replies(), reply(b"maintenance", status=503))
        controller = self.controller(session, transport, ObservationRecovery(gap, 0.1, Mock(), Mock()), poll_seconds=0.01)
        with self.clock(clock), self.assertRaises(ControllerFailed):
            await asyncio.wait_for(controller.run(record()), 3)
        self.assertIsNotNone(session.native.original_exit)
        self.assertIsNone(controller.operation.observed_crash)
        self.assertIsInstance(controller.operation.last_failure.cause, ObservationGapExpired)
        self.assertEqual([], controller.delivery.items)
        self.assertNotIn("execute", session.events)

    async def test_crash_report_does_not_release_failed_retirement_original_io_owner(self):
        session = self.session()
        release = asyncio.Event()
        retirement_failure = ExchangeFailed("Authored observation transport retirement remains owned")

        class HeldRetirement(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                result = await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                if len(self.calls) == 4:
                    self.pending_owner, self.retirement_confirmed = owner, False
                return result

            async def retire(self, *, owner=None):
                if not self.retirement_confirmed and not release.is_set():
                    session.begin_crash.set()
                    await asyncio.wait_for(session.crash_ready.wait(), 2)
                    raise retirement_failure
                await super().retire(owner=owner)

        transport = HeldRetirement(*self.initial_replies(), reply(b"maintenance", status=503))
        controller = self.controller(session, transport, ObservationRecovery(3, 0.1, Mock(), Mock()), poll_seconds=0.01)
        try:
            with self.assertRaises(ControllerFailed):
                await asyncio.wait_for(controller.run(record()), 3)
            self.assertIsNotNone(controller.operation.observed_crash)
            self.assertEqual(1, len(controller.delivery.items))
            exact_body = controller.delivery.items[0].body
            self.assertEqual("worker_crashed", json.loads(exact_body)["failure_code"])
            self.assertIs(transport.pending_owner, controller.operation)
            self.assertFalse(controller.shutdown_ready)
            self.assertTrue(controller.operation.claim.active)
        finally:
            release.set()
            await controller.settle()
        self.assertTrue(controller.shutdown_ready)
        self.assertEqual([exact_body], [item.body for item in controller.delivery.items])
        self.assertEqual(4, len(transport.calls))
        self.assertNotIn("execute", session.events)
