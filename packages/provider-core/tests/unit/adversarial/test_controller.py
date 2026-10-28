"""Failed starts, retained work and retirement never silently release a lifetime."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from tests.fakes.execution import live_controller
from tests.fakes.execution import LiveAttemptProbe
import tempfile
import unittest
from unittest.mock import AsyncMock

from provider_core.producer_checkpoint import SuppressedCheckpoint, TerminalSelectedCheckpoint
from provider_core.controller import ControllerFailed
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.starting import StartFailed
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import observation, start_response
from tests.fakes.controller import SingleRetirementSession, lifecycle_replies
from tests.fakes.execution import Session
from tests.fakes.api import api


class ControllerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_but_stopped_science_requires_explicit_original_executor_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            claims = AttemptClaims.live()
            session = SingleRetirementSession(store)
            transport = Transport(*lifecycle_replies()[:7], reply(observation(job_state="closed")))
            controller = live_controller(api(transport, scope().api_origin), store, claims, session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            with self.assertRaises(ControllerFailed):
                await controller.run(record())
            executor = controller.operation
            with self.assertRaises(ControllerFailed):
                await controller.settle()
            self.assertIs(controller.operation, executor)
            with self.assertRaises(OwnershipRefused):
                claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())
            retained = await executor.retain_suppressed_disposition()
            self.assertIsInstance(retained, SuppressedCheckpoint)
            self.assertIsNone(await controller.settle())
            self.assertEqual(store.records, ())
            self.assertIs(controller.operation._checkpoint, retained)
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            self.assertEqual(len(transport.calls), 8)
            released = claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())
            released.release(owner=released.lifecycle_owner)

    async def test_non_none_retirement_never_confirms_terminal_replay_or_failed_start_cleanup(self):
        for terminal_replay in (False, True):
            with self.subTest(terminal_replay=terminal_replay):
                await self._refuse_non_none_retirement(terminal_replay)

    async def _refuse_non_none_retirement(self, terminal_replay):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            claims = AttemptClaims.live()
            session = Session(store)
            session.retire = AsyncMock(return_value=False)
            start_reply = reply(start_response(state="failed", replayed=True)) if terminal_replay else ExchangeFailed("lost start reply")
            transport = Transport(start_reply)
            controller = live_controller(api(transport, scope().api_origin), store, claims, session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            with self.assertRaises(ControllerFailed) as failure:
                await controller.run(record())
            self.assertIsInstance(failure.exception.cause, TypeError)
            if not terminal_replay:
                self.assertIsInstance(controller.operation.last_failure, StartFailed)
            if terminal_replay:
                session.retire.assert_awaited_once()
            else:
                self.assertEqual(2, session.retire.await_count)
            with self.assertRaises(ControllerFailed):
                await controller.settle()
            with self.assertRaises(OwnershipRefused):
                claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())
            session.retire.return_value = None
            if terminal_replay:
                await controller.settle()
                self.assertFalse(claims.owns_job(scope(), record().input.job_ref))
            else:
                with self.assertRaises(ControllerFailed):
                    await controller.settle()
                self.assertTrue(claims.owns_job(scope(), record().input.job_ref))
            self.assertEqual(len(transport.calls), 1)

    async def test_unaccepted_handoff_keeps_exact_selected_control_without_repeating_science(self):
        from provider_core.delivery_client import DeliveryRejected
        from provider_core.execution import ExecutionFailed
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            claims, session = AttemptClaims.live(), SingleRetirementSession(store)
            delivery = RecordingDelivery()
            failure = DeliveryRejected("Relay acknowledgement did not match the submitted body.")
            delivery.submit = AsyncMock(side_effect=failure)
            transport = Transport(*lifecycle_replies())
            controller = live_controller(api(transport, scope().api_origin), store, claims, session,
                delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            with self.assertRaises(ControllerFailed) as caught:
                await controller.run(record())
            self.assertIsInstance(caught.exception.cause, ExecutionFailed)
            self.assertIs(caught.exception.cause.cause, failure)
            (selected,) = store.records
            self.assertIs(type(selected), TerminalSelectedCheckpoint)
            with self.assertRaises(ControllerFailed):
                await controller.settle()
            delivery.submit.assert_awaited_once()
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            self.assertEqual(len(transport.calls), 8)
            with self.assertRaises(OwnershipRefused):
                claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())

    async def test_invalid_configuration_leaves_session_and_claims_with_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            claims = AttemptClaims.live()
            session = Session(store)
            transport = Transport()
            client = api(transport, scope().api_origin)
            cases = ({"allowed_job_states": frozenset({"cancelled"})}, {"poll_seconds": 0},
                     {"task_settle_seconds": float("nan")}, {"client": api(Transport(), "https://other.example")})
            for change in cases:
                settings = {"client": client, "allowed_job_states": frozenset({"open"}),
                            "poll_seconds": 1, "task_settle_seconds": 0.1} | change
                with self.subTest(change=change), self.assertRaises(ValueError):
                    live_controller(settings.pop("client"), store, claims, session, delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, **settings)
            self.assertEqual(session.events, [])
            self.assertEqual(transport.calls, [])
            self.assertEqual(store.records, ())
            untouched = claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())
            untouched.release(owner=untouched.lifecycle_owner)

    async def test_failed_start_retires_session_and_keeps_claim_until_explicit_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            claims = AttemptClaims.live()
            session = SingleRetirementSession(store)
            transport = Transport(ExchangeFailed("scripted lost start reply"))
            controller = live_controller(api(transport, scope().api_origin), store, claims, session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            deferred = await controller.run(record())
            self.assertTrue(deferred.may_have_committed)
            self.assertEqual(record(), deferred.pending)
            self.assertIsInstance(controller.operation.last_failure, StartFailed)
            self.assertEqual([event[0] for event in session.events], ["retire"])
            with self.assertRaises(OwnershipRefused):
                claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())
            with self.assertRaises(ControllerFailed):
                await controller.settle()
            self.assertTrue(claims.owns_job(scope(), record().input.job_ref))
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(deferred.pending, record())
            self.assertEqual([event[0] for event in session.events], ["retire"])

    async def test_failed_retirement_preserves_primary_and_blocks_exclusion_until_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            claims = AttemptClaims.live()
            session = Session(store)
            cleanup_failure = RuntimeError("scripted unconfirmed child stop")
            session.retirement_failure = cleanup_failure
            controller = live_controller(api(Transport(ExchangeFailed("scripted lost start reply")), scope().api_origin),
                                           store, claims, session, delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                                           poll_seconds=1, task_settle_seconds=0.1)
            with self.assertRaises(ControllerFailed) as failure:
                await controller.run(record())
            primary = failure.exception.cause
            self.assertIs(primary, cleanup_failure)
            self.assertIsInstance(controller.operation.last_failure, StartFailed)
            self.assertIn(cleanup_failure, failure.exception.cleanup_failures)
            with self.assertRaises(ControllerFailed):
                await controller.settle()
            self.assertIs(controller.last_failure.cause, primary)
            with self.assertRaises(OwnershipRefused):
                claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())
            session.retirement_failure = None
            with self.assertRaises(ControllerFailed):
                await controller.settle()
            self.assertTrue(claims.owns_job(scope(), record().input.job_ref))
