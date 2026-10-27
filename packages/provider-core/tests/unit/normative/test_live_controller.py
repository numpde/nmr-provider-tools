"""The real live controller retains uncertain work without restarting science."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from provider_core.controller import ActiveStartReplayHeld, AttemptController, ControllerFailed, LiveStartPolicy, StartDeferred
from provider_core.job_input import verify_job_input
from provider_core.delivery_client import DeliveryUnavailable
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from tests.fakes.api import Transport, api, reply, signature_window_problem
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.job_input import response
from tests.fakes.live_analysis import LiveSession


class LiveControllerTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- Ordinary specimens refuse active replays; the explicit same-live NMR specimen grants its preserved policy.
    def controller(self, transport, *, claims=None, session=None, policy=None, preverified_input=None, allow_active_start_replay=False):
        if claims is None:
            claims = AttemptClaims.live()
        if session is None:
            session = LiveSession()
        if policy is None:
            policy = LiveStartPolicy(cadence_seconds=0.01, on_retry=Mock(), on_recovered=Mock())
        return AttemptController.live(
            api(transport, scope().api_origin), claims, session, scope=scope(), stopping=asyncio.Event(),
            start_policy=policy, preverified_input=preverified_input,
            allow_active_start_replay=allow_active_start_replay,
            delivery=RecordingDelivery(), on_delivery_retry=Mock(), allowed_job_states=frozenset({"open"}),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            poll_seconds=1, task_settle_seconds=0.05,
        )

    def lifecycle(self):
        return Transport(reply(start_response()), reply(observation()), reply(progress_response()), reply(observation()),
                         reply(progress_response(phase="running")), reply(observation()), reply(observation()))

    async def test_full_live_controller_uses_same_input_and_releases_after_delivery_and_cleanup(self):
        claims = AttemptClaims.live()
        verified = verify_job_input(response(), feed=record().input)
        controller = self.controller(self.lifecycle(), claims=claims, preverified_input=verified)
        accepted = await controller.run(record())
        self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
        self.assertIs(verified, controller.session.instructions)
        self.assertEqual(["prepare", "execute", "retire"], controller.session.events)
        self.assertEqual((), claims.live_owners())
        self.assertTrue(controller.work_settled)
        self.assertTrue(controller.shutdown_ready)

    async def test_ack_does_not_release_controller_while_native_cleanup_is_pending(self):
        claims, session = AttemptClaims.live(), LiveSession()
        session.release.clear()
        verified = verify_job_input(response(), feed=record().input)
        controller = self.controller(self.lifecycle(), claims=claims, session=session, preverified_input=verified)
        task = asyncio.create_task(controller.run(record()))
        try:
            await asyncio.wait_for(controller.delivery.accepted.wait(), 1)
            self.assertIn(controller, claims.live_owners())
            self.assertTrue(claims.owns_job(scope(), record().input.job_ref))
            self.assertFalse(controller.work_settled)
        finally:
            session.release.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual((), claims.live_owners())

    async def test_unknown_active_start_replay_cannot_enter_science(self):
        claims = AttemptClaims.live()
        controller = self.controller(Transport(reply(start_response(replayed=True))), claims=claims)
        held = await controller.run(record())
        self.assertIs(type(held), ActiveStartReplayHeld)
        self.assertTrue(held.acknowledgement.admission.replayed)
        self.assertEqual(["retire"], controller.session.events)
        with self.assertRaises(ControllerFailed):
            await controller.settle()
        self.assertIn(controller, claims.live_owners())
        self.assertTrue(controller.work_settled)
        self.assertTrue(controller.shutdown_ready)

    async def test_stop_during_preparation_prevents_scientific_execution(self):
        preparing, release = asyncio.Event(), asyncio.Event()

        class PausedPreparation(LiveSession):
            async def prepare(self, acknowledged, instructions, *, report_condition):
                result = await super().prepare(acknowledged, instructions, report_condition=report_condition)
                preparing.set()
                await release.wait()
                return result

        session = PausedPreparation()
        controller = self.controller(self.lifecycle(), session=session,
                                     preverified_input=verify_job_input(response(), feed=record().input))
        task = asyncio.create_task(controller.run(record()))
        try:
            await asyncio.wait_for(preparing.wait(), 1)
            controller._stopping.set()
        finally:
            release.set()
        with self.assertRaises(ControllerFailed):
            await asyncio.wait_for(task, 1)
        self.assertEqual(["prepare", "retire"], session.events)
        self.assertEqual([], controller.delivery.items)

    async def test_replay_limit_preserves_earlier_commit_uncertainty_after_refusal(self):
        transport = Transport(reply(b"maintenance", status=503), reply(signature_window_problem(), status=401))
        controller = self.controller(transport)
        with patch("provider_core.controller.random.uniform", return_value=1):
            deferred = await controller.run(record())
        self.assertTrue(deferred.may_have_committed)
        self.assertEqual(2, len(transport.calls))
        self.assertEqual(transport.calls[0][0].content, transport.calls[1][0].content)
        self.assertEqual(["retire"], controller.session.events)

    async def test_stop_in_retry_observer_preserves_consumer_cadence_without_another_send(self):
        observed = []
        controller = self.controller(Transport(reply(b"maintenance", status=503)))

        def stop(pending, failure, seconds):
            observed.append(seconds)
            controller._stopping.set()

        controller._start_policy = LiveStartPolicy(cadence_seconds=12, on_retry=stop, on_recovered=Mock())
        deferred = await controller.run(record())
        self.assertEqual([12], observed)
        self.assertTrue(deferred.may_have_committed)
        self.assertEqual(1, len(controller.api._transport.calls))

    async def test_explicit_same_live_retry_runs_science_once_after_exact_replay(self):
        normal = self.lifecycle()
        next(normal.replies)
        transport = Transport(reply(b"maintenance", status=503), reply(start_response(replayed=True)), *normal.replies)
        controller = self.controller(transport, allow_active_start_replay=True,
                                     preverified_input=verify_job_input(response(), feed=record().input))
        with patch("provider_core.controller.random.uniform", return_value=1):
            accepted = await controller.run(record())
        self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
        self.assertEqual(transport.calls[0][0].content, transport.calls[1][0].content)
        self.assertEqual(1, controller.session.events.count("execute"))
        controller._start_policy.on_recovered.assert_called_once()

    async def test_late_active_acknowledgement_stays_owned_without_preparation(self):
        clock = SimpleNamespace(elapsed=0)

        class LateStart(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                result = await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                clock.elapsed = 91
                return result

        controller = self.controller(LateStart(reply(start_response())))
        control_asyncio = SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.elapsed),
            CancelledError=asyncio.CancelledError,
        )
        with patch("provider_core.controller.asyncio", control_asyncio):
            deferred = await controller.run(record())
        self.assertTrue(deferred.may_have_committed)
        self.assertIsNotNone(deferred.acknowledgement)
        self.assertEqual(["retire"], controller.session.events)
        self.assertIn(controller, controller._claims.live_owners())

    async def test_stopped_unacknowledged_report_duty_prevents_clean_shutdown(self):
        class RefusedDelivery(RecordingDelivery):
            async def submit(self, item):
                raise DeliveryUnavailable("No delivery acknowledgement was received", status=400)

        controller = self.controller(self.lifecycle(), preverified_input=verify_job_input(response(), feed=record().input))
        controller.delivery = RefusedDelivery()
        with self.assertRaises(ControllerFailed):
            await controller.run(record())
        self.assertTrue(controller.work_settled)
        self.assertFalse(controller.shutdown_ready)
        self.assertIsNotNone(controller.operation._delivery_outcome)
        with self.assertRaises(ControllerFailed):
            await controller.settle()
        self.assertTrue(controller.work_settled)
        self.assertFalse(controller.shutdown_ready)

    async def test_capacity_refusal_is_typed_and_rejected_controller_can_settle(self):
        claims = AttemptClaims.live(limits=LiveAttemptLimits(maximum_control_bytes=1))
        transport = self.lifecycle()
        controller = self.controller(transport, claims=claims)
        with self.assertRaises(ControllerFailed) as refused:
            await controller.run(record())
        self.assertIs(type(refused.exception.cause), LiveCapacityFull)
        self.assertEqual([], transport.calls)
        await controller.settle()
        self.assertTrue(controller.shutdown_ready)
        self.assertEqual((), claims.live_owners())

    async def test_known_uncommitted_start_deferral_can_release_after_cleanup(self):
        claims = AttemptClaims.live()
        policy = LiveStartPolicy(cadence_seconds=1, maximum_replays=0, on_retry=Mock(), on_recovered=Mock())
        transport = Transport(reply(signature_window_problem(), status=401))
        controller = self.controller(transport, claims=claims, policy=policy)
        deferred = await controller.run(record())
        self.assertIs(type(deferred), StartDeferred)
        self.assertFalse(deferred.may_have_committed)
        self.assertEqual(["retire"], controller.session.events)
        self.assertEqual((), claims.live_owners())

    async def test_uncertain_start_stays_held_when_retry_cannot_fit_decision_window(self):
        claims = AttemptClaims.live()
        policy = LiveStartPolicy(cadence_seconds=1, window_seconds=1, on_retry=Mock(), on_recovered=Mock())
        transport = Transport(reply(b"maintenance", status=503))
        controller = self.controller(transport, claims=claims, policy=policy)
        deferred = await controller.run(record())
        self.assertIs(type(deferred), StartDeferred)
        self.assertTrue(deferred.may_have_committed)
        self.assertEqual(1, len(transport.calls))
        with self.assertRaises(ControllerFailed):
            await controller.settle()
        self.assertIn(controller, claims.live_owners())
        self.assertTrue(controller.work_settled)
