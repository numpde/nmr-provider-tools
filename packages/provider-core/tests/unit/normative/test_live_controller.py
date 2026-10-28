"""The real live controller retains uncertain work without restarting science."""

import asyncio
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpcore

from provider_core.controller import ActiveStartReplayHeld, AttemptController, ControllerFailed, ControllerRefused, LiveStartPolicy, StartDeferred
from provider_core.job_input import verify_job_input
from provider_core.delivery_client import DeliveryUnavailable
from provider_core.delivery_store import DeliveryFull
from provider_core.ownership import AttemptClaims, LiveAttemptLimits, LiveCapacityFull
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, api, reply, response_headers, signature_window_problem
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.job_input import response
from tests.fakes.live_analysis import LiveSession


class LiveControllerTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- Ordinary specimens refuse active replays; the explicit same-live NMR specimen grants its preserved policy.
    def controller(self, transport, *, claims=None, session=None, policy=None, preverified_input=None, allow_active_start_replay=False, stopping=None, admission_stopping=None):
        if claims is None:
            claims = AttemptClaims.live()
        if session is None:
            session = LiveSession()
        if policy is None:
            policy = LiveStartPolicy(cadence_seconds=0.01, on_retry=Mock(), on_recovered=Mock())
        if stopping is None:
            stopping = asyncio.Event()
        return AttemptController.live(
            api(transport, scope().api_origin), claims, session, scope=scope(), stopping=stopping,
            start_policy=policy, preverified_input=preverified_input,
            admission_stopping=admission_stopping,
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

    async def test_cancelled_observer_keeps_original_selected_report_retrying(self):
        pressured, recovered = asyncio.Event(), asyncio.Event()
        bodies = []

        class PressuredDelivery(RecordingDelivery):
            async def submit(self, item):
                bodies.append(item.body)
                if not recovered.is_set():
                    pressured.set()
                    raise DeliveryFull("The authored queue is full")
                return await super().submit(item)

        claims = AttemptClaims.live()
        controller = self.controller(
            self.lifecycle(), claims=claims,
            preverified_input=verify_job_input(response(), feed=record().input),
        )
        controller.delivery = delivery = PressuredDelivery()
        observer = asyncio.create_task(controller.run(record()))
        try:
            await asyncio.wait_for(pressured.wait(), 1)
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await observer
            self.assertIn(controller, claims.live_owners())
            self.assertFalse(controller.shutdown_ready)
            joined_observer = asyncio.create_task(controller.join())
            await asyncio.sleep(0)
            joined_observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await joined_observer
            recovered.set()
            await asyncio.wait_for(delivery.accepted.wait(), 1)
            accepted = await asyncio.wait_for(controller.join(), 1)
            self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
            self.assertIs(accepted, await controller.join())
            self.assertTrue(controller.shutdown_ready)
            self.assertEqual((), claims.live_owners())
            self.assertEqual(controller.session.events.count("execute"), 1)
            self.assertGreaterEqual(len(bodies), 2)
            self.assertTrue(all(body == bodies[0] for body in bodies))
        finally:
            recovered.set()
            if not observer.done():
                await observer
            await asyncio.wait_for(asyncio.gather(controller.join(), return_exceptions=True), 2)

    async def test_claim_is_visible_when_observer_cancels_before_driver_scheduling(self):
        claims, session = AttemptClaims.live(), LiveSession()
        session.release.clear()
        controller = self.controller(self.lifecycle(), claims=claims, session=session,
                                     preverified_input=verify_job_input(response(), feed=record().input))
        ready, created = asyncio.Event(), []
        create_task = asyncio.create_task

        async def observe():
            await ready.wait()
            return await controller.run(record())

        observer = create_task(observe())

        def spawn(coroutine):
            if not created:
                self.assertIn(controller, claims.live_owners())
                asyncio.current_task().cancel()
            task = create_task(coroutine)
            created.append(task)
            return task

        try:
            with patch("provider_core.controller.asyncio.create_task", side_effect=spawn):
                ready.set()
                with self.assertRaises(asyncio.CancelledError):
                    await observer
            self.assertIn(controller, claims.live_owners())
            self.assertFalse(controller.shutdown_ready)
        finally:
            session.release.set()
            await asyncio.wait_for(controller.join(), 2)
        self.assertEqual(controller.session.events.count("execute"), 1)

    async def test_failed_driver_creation_retains_resources_until_no_effect_settlement(self):
        claims, failure = AttemptClaims.live(), RuntimeError("Cannot create the authored driver")
        transport = self.lifecycle()
        controller = self.controller(transport, claims=claims)
        with patch("provider_core.controller.asyncio.create_task", side_effect=failure):
            with self.assertRaises(ControllerFailed) as caught:
                await controller.run(record())
        self.assertIs(caught.exception.cause, failure)
        self.assertEqual(transport.calls, [])
        self.assertEqual(controller.session.events, ["retire"])
        self.assertIn(controller, claims.live_owners())
        with self.assertRaises(ControllerRefused):
            await controller.join()
        await controller.settle()
        self.assertEqual(claims.live_owners(), ())

    async def test_local_stop_does_not_set_the_shared_supplied_event(self):
        supplied, claims = asyncio.Event(), AttemptClaims.live()
        stopped = self.controller(self.lifecycle(), claims=claims, stopping=supplied)
        stopped.request_stop()
        deferred = await stopped.run(record())
        self.assertFalse(deferred.may_have_committed)
        self.assertFalse(supplied.is_set())
        self.assertEqual(stopped.api._transport.calls, [])
        sibling = self.controller(self.lifecycle(), claims=claims, stopping=supplied,
                                  preverified_input=verify_job_input(response(), feed=record().input))
        self.assertIs(type(await sibling.run(record())), DeliveryAcceptedCheckpoint)

    async def test_admission_stop_after_cancelled_observation_prevents_replay_or_late_science(self):
        for receipt in (reply(b"maintenance", status=503), reply(start_response())):
            with self.subTest(status=receipt.status):
                entered, release, admission = asyncio.Event(), asyncio.Event(), asyncio.Event()

                class PausedStart(Transport):
                    async def exchange(self, request, *, maximum_bytes, owner=None):
                        entered.set()
                        await release.wait()
                        return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

                transport = PausedStart(receipt)
                controller = self.controller(transport, admission_stopping=admission)
                observer = asyncio.create_task(controller.run(record()))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    observer.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await observer
                    admission.set()
                    release.set()
                    held = await asyncio.wait_for(controller.join(), 1)
                    self.assertIs(type(held), StartDeferred)
                    self.assertTrue(held.may_have_committed)
                    self.assertEqual(len(transport.calls), 1)
                    self.assertEqual(controller.session.events, ["retire"])
                    self.assertFalse(controller._stopping.is_set())
                finally:
                    release.set()
                    admission.set()
                    await asyncio.wait_for(asyncio.gather(controller.join(), return_exceptions=True), 2)

    async def test_admission_stop_does_not_revoke_already_admitted_science(self):
        preparing, release, admission = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class PausedPreparation(LiveSession):
            async def prepare(self, acknowledged, instructions, *, report_condition):
                result = await super().prepare(acknowledged, instructions, report_condition=report_condition)
                preparing.set()
                await release.wait()
                return result

        controller = self.controller(
            self.lifecycle(), session=PausedPreparation(), admission_stopping=admission,
            preverified_input=verify_job_input(response(), feed=record().input),
        )
        observer = asyncio.create_task(controller.run(record()))
        try:
            await asyncio.wait_for(preparing.wait(), 1)
            admission.set()
        finally:
            release.set()
        self.assertIs(type(await asyncio.wait_for(observer, 1)), DeliveryAcceptedCheckpoint)
        self.assertEqual(controller.session.events, ["prepare", "execute", "retire"])

    async def test_start_owner_creation_failure_keeps_a_releasable_no_effect_claim(self):
        claims, failure = AttemptClaims.live(), RuntimeError("Cannot construct the START owner")
        controller = self.controller(self.lifecycle(), claims=claims)
        with patch("provider_core.controller.AttemptStarter.live", side_effect=failure):
            with self.assertRaises(ControllerFailed) as caught:
                await controller.run(record())
        self.assertIs(caught.exception.cause, failure)
        self.assertEqual(controller.api._transport.calls, [])
        self.assertIn(controller, claims.live_owners())
        await controller.settle()
        self.assertEqual(claims.live_owners(), ())

    async def test_second_stop_waiter_creation_failure_joins_the_first_waiter(self):
        failure, created, calls = RuntimeError("Cannot create the second stop waiter"), [], []
        controller = self.controller(Transport(reply(b"maintenance", status=503)))
        create_task = asyncio.create_task

        def spawn(coroutine):
            calls.append(coroutine)
            if len(calls) == 3:
                raise failure
            task = create_task(coroutine)
            created.append(task)
            return task

        with patch("provider_core.controller.asyncio.create_task", side_effect=spawn):
            with self.assertRaises(ControllerFailed) as caught:
                await controller.run(record())
        self.assertIs(caught.exception.cause, failure)
        self.assertEqual(len(controller.api._transport.calls), 1)
        self.assertEqual(len(created), 2)
        self.assertTrue(all(task.done() for task in created))

    @unittest.skipUnless(hasattr(asyncio, "eager_task_factory"), "This Python has no eager task factory")
    async def test_eager_driver_dispatch_uses_the_registered_claim(self):
        controller = self.controller(self.lifecycle(), preverified_input=verify_job_input(response(), feed=record().input))
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            self.assertIs(type(await controller.run(record())), DeliveryAcceptedCheckpoint)
        finally:
            loop.set_task_factory(previous)
        self.assertTrue(controller.shutdown_ready)

    async def test_unknown_active_start_replay_cannot_enter_science(self):
        claims = AttemptClaims.live()
        controller = self.controller(Transport(reply(start_response(replayed=True))), claims=claims)
        held = await controller.run(record())
        self.assertIs(type(held), ActiveStartReplayHeld)
        self.assertTrue(held.acknowledgement.admission.replayed)
        self.assertEqual(["retire"], controller.session.events)
        self.assertIsNone(await controller.settle())
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
        for header in ("signature-input", "signature"):
            self.assertNotEqual(transport.calls[0][0].headers[header], transport.calls[1][0].headers[header])
        self.assertEqual(1, controller.session.events.count("execute"))
        controller._start_policy.on_recovered.assert_called_once()

    async def test_permanent_malformed_or_unsettled_start_failure_never_replays(self):
        for response in (
            reply(b"denied", status=401), reply(b"conflict", status=409), reply(b"invalid success"),
            reply(b"maintenance", status=503, headers=response_headers(status=503, topology=b"web")),
            ExchangeFailed("Certificate rejected", primary=ssl.SSLCertVerificationError()),
            ExchangeFailed("Cleanup failed", primary=httpcore.ReadTimeout(), cleanup_failures=(OSError(),)),
        ):
            with self.subTest(response_type=type(response).__name__):
                controller = self.controller(Transport(response))
                deferred = await controller.run(record())
                self.assertIs(type(deferred), StartDeferred)
                self.assertEqual(record(), deferred.pending)
                self.assertEqual(1, len(controller.api._transport.calls))
                controller._start_policy.on_retry.assert_not_called()
                self.assertEqual(["retire"], controller.session.events)

    async def test_explicit_stop_during_retry_wait_preserves_original_command(self):
        waiting = asyncio.Event()
        controller = self.controller(Transport(reply(b"maintenance", status=503)))
        controller._start_policy = LiveStartPolicy(10, lambda *_: waiting.set(), Mock())
        task = asyncio.create_task(controller.run(record()))
        await asyncio.wait_for(waiting.wait(), 1)
        controller.request_stop()
        deferred = await asyncio.wait_for(task, 1)
        self.assertIs(type(deferred), StartDeferred)
        self.assertEqual(record(), controller._claim._pending)
        self.assertEqual(1, len(controller.api._transport.calls))
        self.assertEqual(["retire"], controller.session.events)
        self.assertIn(controller, controller._claims.live_owners())
        for _ in range(2):
            self.assertIsNone(await controller.settle())
            self.assertTrue(controller.shutdown_ready)
            self.assertIn(controller, controller._claims.live_owners())
        self.assertEqual(["retire"], controller.session.events)

    async def test_original_retirement_and_late_evidence_precede_replay(self):
        for late in ("certificate", "cleanup", "availability"):
            with self.subTest(late=late):
                lost = ExchangeFailed("Unconfirmed START", primary=httpcore.ReadTimeout())
                transport = Transport(lost, reply(start_response(state="failed", replayed=True)))
                exchange, retire = transport.exchange, transport.retire
                entered, release = asyncio.Event(), asyncio.Event()

                async def held_exchange(request, *, maximum_bytes, owner=None):
                    try:
                        return await exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                    except ExchangeFailed:
                        transport.retirement_confirmed, transport.pending_owner = False, owner
                        raise

                async def held_retirement(*, owner=None):
                    entered.set()
                    await release.wait()
                    if late == "cleanup":
                        raise ExchangeFailed("Retirement failed", primary=OSError())
                    if late == "certificate":
                        lost.primary = ssl.SSLCertVerificationError()
                    await retire(owner=owner)

                transport.exchange, transport.retire = held_exchange, held_retirement
                controller = self.controller(transport)
                task = asyncio.create_task(controller.run(record()))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertEqual(1, len(transport.calls))
                    self.assertFalse(task.done())
                    release.set()
                    if late == "cleanup":
                        with self.assertRaises(ControllerFailed):
                            await asyncio.wait_for(task, 3)
                    else:
                        await asyncio.wait_for(task, 3)
                    if late == "availability":
                        self.assertEqual(2, len(transport.calls))
                    else:
                        self.assertEqual(1, len(transport.calls))
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_late_active_acknowledgement_stays_owned_without_preparation(self):
        clock = SimpleNamespace(elapsed=0)

        class LateStart(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                result = await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                clock.elapsed = 91
                return result

        controller = self.controller(LateStart(reply(start_response())))
        control_asyncio = Mock(wraps=asyncio)
        control_asyncio.get_running_loop.return_value = SimpleNamespace(time=lambda: clock.elapsed)
        with patch("provider_core.controller.asyncio", control_asyncio):
            deferred = await controller.run(record())
        self.assertTrue(deferred.may_have_committed)
        self.assertIsNotNone(deferred.acknowledgement)
        self.assertEqual(["retire"], controller.session.events)
        self.assertIn(controller, controller._claims.live_owners())
        for _ in range(2):
            self.assertIsNone(await controller.settle())
            self.assertTrue(controller.shutdown_ready)
            self.assertIn(controller, controller._claims.live_owners())
        self.assertIsNone(controller.last_failure)
        self.assertEqual(["retire"], controller.session.events)

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
        self.assertIsNone(await controller.settle())
        self.assertIn(controller, claims.live_owners())
        self.assertTrue(controller.work_settled)
        self.assertTrue(controller.shutdown_ready)
