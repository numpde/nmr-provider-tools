"""Failed starts, retained work and retirement never silently release a lifetime."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
import tempfile
import unittest
from unittest.mock import AsyncMock

from provider_core.producer_checkpoint import SuppressedCheckpoint, DeliveryAcceptedCheckpoint, TerminalSelectedCheckpoint
from provider_core.controller import AttemptController, ControllerFailed, ControllerRefused
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.recovery import AttemptRecovery
from provider_core.starting import StartFailed
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import complete_response, observation, start_response
from tests.fakes.controller import SingleRetirementSession, empty_store, lifecycle_replies
from tests.fakes.execution import Session
from tests.fakes.api import api


class ControllerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_but_stopped_science_requires_explicit_original_executor_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            claims = AttemptClaims()
            session = SingleRetirementSession(store)
            transport = Transport(*lifecycle_replies()[:7], reply(observation(job_state="closed")))
            controller = AttemptController(api(transport, scope().api_origin), store, claims, session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                with self.assertRaises(ControllerFailed):
                    await controller.run(record())
                executor = controller.operation
                with self.assertRaises(ControllerFailed):
                    await controller.settle()
                self.assertIs(controller.operation, executor)
                with self.assertRaises(OwnershipRefused):
                    claims.claim_start(record())
                retained = await executor.retain_suppressed_disposition()
                self.assertIsInstance(retained, SuppressedCheckpoint)
                self.assertIsNone(await controller.settle())
                self.assertEqual(await store.inventory(), (retained,))
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(len(transport.calls), 8)
                released = claims.claim_start(record())
                released.release()
            finally:
                await store.close()

    async def test_non_none_retirement_never_confirms_terminal_replay_or_failed_start_cleanup(self):
        for terminal_replay in (False, True):
            with self.subTest(terminal_replay=terminal_replay):
                await self._refuse_non_none_retirement(terminal_replay)

    async def _refuse_non_none_retirement(self, terminal_replay):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            claims = AttemptClaims()
            session = Session(store)
            session.retire = AsyncMock(return_value=False)
            start_reply = reply(start_response(state="failed", replayed=True)) if terminal_replay else ExchangeFailed("lost start reply")
            transport = Transport(start_reply)
            controller = AttemptController(api(transport, scope().api_origin), store, claims, session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                with self.assertRaises(ControllerFailed) as failure:
                    await controller.run(record())
                if terminal_replay:
                    self.assertIsInstance(failure.exception.cause, TypeError)
                else:
                    self.assertIsInstance(failure.exception.cause, StartFailed)
                    self.assertTrue(any(isinstance(cause, TypeError) for cause in failure.exception.cleanup_failures))
                session.retire.assert_awaited_once()
                with self.assertRaises(ControllerFailed):
                    await controller.settle()
                with self.assertRaises(OwnershipRefused):
                    claims.claim_start(record())
                session.retire.return_value = None
                await controller.settle()
                released = claims.claim_start(record())
                released.release()
                self.assertEqual(len(transport.calls), 1)
            finally:
                await store.close()

    async def test_unaccepted_handoff_keeps_exact_selected_control_without_repeating_science(self):
        from provider_core.delivery_client import DeliveryRejected
        from provider_core.execution import ExecutionFailed
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            claims, session = AttemptClaims(), SingleRetirementSession(store)
            delivery = RecordingDelivery()
            failure = DeliveryRejected("Relay acknowledgement did not match the submitted body.")
            delivery.submit = AsyncMock(side_effect=failure)
            transport = Transport(*lifecycle_replies())
            controller = AttemptController(api(transport, scope().api_origin), store, claims, session,
                delivery=delivery, on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                with self.assertRaises(ControllerFailed) as caught:
                    await controller.run(record())
                self.assertIsInstance(caught.exception.cause, ExecutionFailed)
                self.assertIs(caught.exception.cause.cause, failure)
                (selected,) = await store.inventory()
                self.assertIs(type(selected), TerminalSelectedCheckpoint)
                with self.assertRaises(ControllerFailed):
                    await controller.settle()
                delivery.submit.assert_awaited_once()
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(len(transport.calls), 8)
                with self.assertRaises(OwnershipRefused):
                    claims.claim_start(record())
            finally:
                await store.close()

    async def test_invalid_configuration_leaves_session_and_claims_with_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            claims = AttemptClaims()
            session = Session(store)
            transport = Transport()
            client = api(transport, scope().api_origin)
            cases = ({"allowed_job_states": frozenset({"cancelled"})}, {"poll_seconds": 0},
                     {"task_settle_seconds": float("nan")}, {"client": api(Transport(), "https://other.example")})
            try:
                for change in cases:
                    settings = {"client": client, "allowed_job_states": frozenset({"open"}),
                                "poll_seconds": 1, "task_settle_seconds": 0.1} | change
                    with self.subTest(change=change), self.assertRaises(ValueError):
                        AttemptController(settings.pop("client"), store, claims, session, delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, **settings)
                self.assertEqual(session.events, [])
                self.assertEqual(transport.calls, [])
                self.assertEqual(await store.inventory(), ())
                untouched = claims.claim_start(record())
                untouched.release()
            finally:
                await store.close()

    async def test_failed_start_retires_session_and_keeps_claim_until_explicit_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            claims = AttemptClaims()
            session = SingleRetirementSession(store)
            transport = Transport(ExchangeFailed("scripted lost start reply"))
            controller = AttemptController(api(transport, scope().api_origin), store, claims, session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                with self.assertRaises(ControllerFailed) as failure:
                    await controller.run(record())
                self.assertIsInstance(failure.exception.cause, StartFailed)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                with self.assertRaises(OwnershipRefused):
                    claims.claim_start(record())
                self.assertIsNone(await controller.settle())
                released = claims.claim_start(record())
                released.release()
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(await store.inventory(), (record(),))
                self.assertEqual([event[0] for event in session.events], ["retire"])
            finally:
                await store.close()

    async def test_failed_retirement_preserves_primary_and_blocks_exclusion_until_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            claims = AttemptClaims()
            session = Session(store)
            cleanup_failure = RuntimeError("scripted unconfirmed child stop")
            session.retirement_failure = cleanup_failure
            controller = AttemptController(api(Transport(ExchangeFailed("scripted lost start reply")), scope().api_origin),
                                           store, claims, session, delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                                           poll_seconds=1, task_settle_seconds=0.1)
            try:
                with self.assertRaises(ControllerFailed) as failure:
                    await controller.run(record())
                primary = failure.exception.cause
                self.assertIsInstance(primary, StartFailed)
                self.assertIn(cleanup_failure, failure.exception.cleanup_failures)
                with self.assertRaises(ControllerFailed):
                    await controller.settle()
                self.assertIs(controller.last_failure.cause, primary)
                with self.assertRaises(OwnershipRefused):
                    claims.claim_start(record())
                session.retirement_failure = None
                self.assertIsNone(await controller.settle())
                released = claims.claim_start(record())
                released.release()
            finally:
                await store.close()

    async def test_unresolved_inventory_refuses_fresh_start_and_retires_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=2)
            prior = record("prior:start")
            await store.retain_start(prior)
            session = Session(store)
            transport = Transport()
            controller = AttemptController(api(transport, scope().api_origin), store, AttemptClaims(), session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                with self.assertRaises(ControllerFailed) as failure:
                    await controller.run(record())
                self.assertIsInstance(failure.exception.cause, ControllerRefused)
                self.assertEqual(transport.calls, [])
                self.assertEqual(await store.inventory(), (prior,))
                self.assertEqual([event[0] for event in session.events], ["retire"])
                self.assertIsNone(await controller.settle())
            finally:
                await store.close()
