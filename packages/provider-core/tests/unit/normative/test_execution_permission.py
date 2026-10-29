"""A phase checks current local authority at the actual dispatch boundary."""

import asyncio
import unittest
from unittest.mock import Mock, patch

from provider_core.controller import AttemptController, ControllerFailed, LiveStartPolicy
from provider_core.execution import ExecutionRefused, ExecutionStopRequested, ObservationGapExpired, ObservationRecovery
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempt_store import record
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.controller import lifecycle_replies
from tests.fakes.execution import LiveAttemptProbe, Session
from tests.fakes.attempts import start_response
from tests.fakes.conditions import ConditionTransport
from provider_core.transport import ExchangeFailed
from provider_core.ownership import AttemptClaims


def controlled(session_type, stopping, admission_stopping):
    probe = LiveAttemptProbe()
    session, delivery = session_type(probe), RecordingDelivery()
    transport = Transport(*lifecycle_replies())
    controller = AttemptController.live(
        api(transport, probe.scope.api_origin), AttemptClaims.live(), session,
        scope=probe.scope, stopping=stopping, admission_stopping=admission_stopping,
        start_policy=LiveStartPolicy(0.001, Mock(), Mock()),
        delivery=delivery, on_delivery_retry=Mock(),
        delivery_retry_initial_seconds=0.001, delivery_retry_max_seconds=0.002,
        allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1,
    )
    probe.controller = controller
    return controller, session, delivery, transport


class WaitingDispatch(Session):
    def __init__(self, probe):
        super().__init__(probe)
        self.waiting, self.release = asyncio.Event(), asyncio.Event()
        self.dispatches = 0
        self.checked = False
        self.control = None

    async def execute(self, prepared, *, report_condition):
        self.control = report_condition
        self.waiting.set()
        await self.release.wait()
        self.checked = True
        report_condition.require_execution()
        # No yield between permission and this recording dispatch commitment.
        self.dispatches += 1
        return await super().execute(prepared, report_condition=report_condition)


class ExecutionPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_scientific_stop_authorities_veto_a_waited_dispatch(self):
        for authority in ("event", "controller"):
            with self.subTest(authority=authority):
                stopping, admission = asyncio.Event(), asyncio.Event()
                controller, session, delivery, _ = controlled(WaitingDispatch, stopping, admission)
                running = asyncio.create_task(controller.run(record()))
                try:
                    await asyncio.wait_for(session.waiting.wait(), 2)
                    if authority == "event":
                        stopping.set()
                    else:
                        controller.request_stop()
                    session.release.set()
                    with self.assertRaises(ControllerFailed) as caught:
                        await asyncio.wait_for(running, 2)
                    self.assertTrue(session.checked)
                    self.assertIs(type(caught.exception.cause.cause), ExecutionStopRequested)
                    self.assertEqual(session.dispatches, 0)
                    self.assertEqual(delivery.items, [])
                    self.assertTrue(controller.shutdown_ready)
                finally:
                    session.release.set()
                    controller.request_stop()
                    if not running.done():
                        running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    if controller._driver is not None:
                        await asyncio.gather(controller._driver, return_exceptions=True)

    async def test_admission_stop_allows_already_admitted_science_and_permit_expires(self):
        stopping, admission = asyncio.Event(), asyncio.Event()
        controller, session, delivery, transport = controlled(WaitingDispatch, stopping, admission)
        running = asyncio.create_task(controller.run(record()))
        try:
            await asyncio.wait_for(session.waiting.wait(), 2)
            admission.set()
            session.release.set()
            await asyncio.wait_for(running, 2)
            self.assertEqual(session.dispatches, 1)
            self.assertEqual(len(delivery.items), 1)
            calls = len(transport.calls)
            with self.assertRaises(ExecutionRefused):
                session.control.require_execution()
            self.assertEqual(len(transport.calls), calls)
        finally:
            session.release.set()
            controller.request_stop()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            if controller._driver is not None:
                await asyncio.gather(controller._driver, return_exceptions=True)


class LiveConditionTransport(ConditionTransport):
    async def exchange(self, request, *, maximum_bytes, owner=None):
        if request.method == "POST" and request.url.path.endswith("/start"):
            self.calls.append((request, maximum_bytes))
            return reply(start_response())
        return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)


def composed(session_type, transport, *, observation_recovery, poll_seconds):
    probe = LiveAttemptProbe()
    session, delivery = session_type(probe), RecordingDelivery()
    controller = AttemptController.live(
        api(transport, probe.scope.api_origin), AttemptClaims.live(), session,
        scope=probe.scope, stopping=asyncio.Event(), admission_stopping=asyncio.Event(),
        start_policy=LiveStartPolicy(0.001, Mock(), Mock()),
        delivery=delivery, on_delivery_retry=Mock(),
        delivery_retry_initial_seconds=0.001, delivery_retry_max_seconds=0.002,
        allowed_job_states=frozenset({"open"}), poll_seconds=poll_seconds,
        task_settle_seconds=0.1, observation_recovery=observation_recovery,
    )
    probe.controller = controller
    return controller, session, delivery


class ChildDispatch(Session):
    async def checked_child(self, report_condition, phase):
        await asyncio.sleep(0)
        report_condition.require_execution()
        self.dispatches.append(phase)

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.dispatches = []
        await asyncio.create_task(self.checked_child(report_condition, "prepare"))
        return await super().prepare(acknowledged, instructions, report_condition=report_condition)

    async def execute(self, prepared, *, report_condition):
        await asyncio.create_task(self.checked_child(report_condition, "execute"))
        return await super().execute(prepared, report_condition=report_condition)


class CaughtReportFailure(Session):
    async def prepare(self, acknowledged, instructions, *, report_condition):
        prepared = await super().prepare(acknowledged, instructions, report_condition=report_condition)
        self.dispatches = 0
        try:
            await report_condition("waiting_capacity")
        except Exception as original:
            self.report_failure = original
        try:
            report_condition.require_execution()
        except Exception as refused:
            self.dispatch_failure = refused
        else:
            self.dispatches += 1
        return prepared


class CaughtInFlightReport(Session):
    async def prepare(self, acknowledged, instructions, *, report_condition):
        prepared = await super().prepare(acknowledged, instructions, report_condition=report_condition)
        self.dispatches = 0
        task = asyncio.create_task(report_condition("waiting_capacity"))
        try:
            await self.transport.blocked.wait()
            try:
                report_condition.require_execution()
            except ExecutionRefused as refusal:
                self.dispatch_failure = refusal
            else:
                self.dispatches += 1
        finally:
            self.transport.release.set()
            # The child is owned and joined even when its poisoned report raises.
            self.report_results = await asyncio.gather(task, return_exceptions=True)
        return prepared


class DeadlineDispatch(Session):
    async def execute(self, prepared, *, report_condition):
        self.dispatches = 0
        await self.transport.read_blocked.wait()
        executor = self.probe.controller.operation
        deadline = executor._observation_deadline
        self.pending_read = executor._observation_wait
        self.read_was_pending = self.pending_read is not None and not self.pending_read.done()
        self.asserted_deadline = deadline
        loop = asyncio.get_running_loop()
        # No await while advancing time: scheduled timeout callbacks cannot run.
        with patch.object(loop, "time", return_value=deadline):
            try:
                report_condition.require_execution()
            except ObservationGapExpired as failure:
                self.dispatch_failure = failure
                self.transport.release_read.set()
                raise
            else:
                self.dispatches += 1
        self.transport.release_read.set()
        return await super().execute(prepared, report_condition=report_condition)


class ExecutionPermissionAdditionalTests(unittest.IsolatedAsyncioTestCase):
    async def run_controller(self, controller, transport):
        running = asyncio.create_task(controller.run(record()))
        try:
            return await asyncio.wait_for(running, 2)
        finally:
            transport.release.set()
            transport.release_read.set()
            controller.request_stop()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            if controller._driver is not None:
                await asyncio.gather(controller._driver, return_exceptions=True)

    async def test_owned_awaited_child_tasks_can_dispatch_in_both_phases(self):
        transport = LiveConditionTransport()
        controller, session, delivery = composed(ChildDispatch, transport, observation_recovery=None, poll_seconds=1)
        await self.run_controller(controller, transport)
        self.assertEqual(session.dispatches, ["prepare", "execute"])
        self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
        self.assertEqual(len(delivery.items), 1)
        self.assertTrue(controller.shutdown_ready)

    async def test_caught_actual_condition_failure_poison_blocks_next_scientific_effect(self):
        transport = LiveConditionTransport()
        transport.failure = ExchangeFailed("PRIVATE_REPORT_FAILURE")
        transport.release.set()
        controller, session, delivery = composed(CaughtReportFailure, transport, observation_recovery=None, poll_seconds=1)
        with self.assertRaises(ControllerFailed) as caught:
            await self.run_controller(controller, transport)
        self.assertIs(session.dispatch_failure, session.report_failure)
        self.assertIs(caught.exception.cause.cause, session.report_failure)
        self.assertEqual(session.dispatches, 0)
        self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
        self.assertEqual(delivery.items, [])
        self.assertTrue(controller.shutdown_ready)

    async def test_caught_in_flight_condition_refusal_cannot_publish_after_report_settles(self):
        transport = LiveConditionTransport()
        controller, session, delivery = composed(CaughtInFlightReport, transport, observation_recovery=None, poll_seconds=1)
        session.transport = transport
        with self.assertRaises(ControllerFailed) as caught:
            await self.run_controller(controller, transport)
        self.assertIs(type(session.dispatch_failure), ExecutionRefused)
        self.assertIs(caught.exception.cause.cause, session.dispatch_failure)
        self.assertEqual(session.dispatches, 0)
        self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
        self.assertEqual(delivery.items, [])
        self.assertTrue(controller.shutdown_ready)

    async def test_expired_actual_observation_window_blocks_dispatch_before_timer_callback(self):
        transport = LiveConditionTransport()
        # First three reads are admission/preparation/entry observations; the
        # fourth is the actual monitor read while execute awaits dispatch.
        transport.block_read = 4
        recovery = ObservationRecovery(1, 0.1, Mock(), Mock())
        controller, session, delivery = composed(DeadlineDispatch, transport,
            observation_recovery=recovery, poll_seconds=0.001)
        session.transport = transport
        with self.assertRaises(ControllerFailed) as caught:
            await self.run_controller(controller, transport)
        self.assertIsNotNone(session.asserted_deadline)
        self.assertIsNotNone(session.pending_read)
        self.assertTrue(session.read_was_pending)
        self.assertIs(caught.exception.cause.cause, session.dispatch_failure)
        self.assertIs(session.dispatch_failure._executor, controller.operation)
        self.assertEqual(session.dispatches, 0)
        self.assertEqual(delivery.items, [])
        self.assertTrue(controller.shutdown_ready)
