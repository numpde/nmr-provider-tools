"""The runtime owner joins existing effects and releases only retired lifetimes."""

from tests.fakes.checkpoint_execution import RecordingDelivery
from unittest.mock import Mock
from tests.fakes.execution import live_controller
from tests.fakes.execution import LiveAttemptProbe
import tempfile
import unittest

from provider_core.attempt_record import AcknowledgedAttempt
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.controller import ControllerRefused
from provider_core.ownership import AttemptClaims, OwnershipRefused
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import start_response
from tests.fakes.controller import lifecycle_replies
from tests.fakes.execution import Session
from tests.fakes.api import api


class ControllerTests(unittest.IsolatedAsyncioTestCase):

    async def test_claim_exposes_only_its_current_authoritative_operation_owner(self):
        claim = AttemptClaims.live().claim_live(record(), lifecycle_owner=LiveAttemptProbe())
        self.assertIsNone(claim.operation_owner)
        owner = object()
        claim.begin(owner)
        self.assertIs(claim.operation_owner, owner)
        claim.finish(owner)
        self.assertIsNone(claim.operation_owner)
        claim.release(owner=claim.lifecycle_owner)
        with self.assertRaises(OwnershipRefused):
            _ = claim.operation_owner

    async def test_confirmed_inventory_does_not_globally_block_another_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            claims = AttemptClaims.live()
            first = live_controller(api(Transport(*lifecycle_replies()), scope().api_origin), store, claims, Session(store),
                                      delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            confirmed = await first.run(record())
            session = Session(store)
            transport = Transport(reply(start_response(state="failed", replayed=True,
                                                       execution_attempt_ref="execution_attempt:sha256:" + "b" * 64)))
            second = live_controller(api(transport, scope().api_origin), store, claims, session,
                                       delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            terminal = await second.run(record("second:start"))
            self.assertIsInstance(terminal, AcknowledgedAttempt)
            self.assertEqual(store.records, ())
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual([event[0] for event in session.events], ["retire"])

    async def test_one_run_retains_confirmation_and_releases_private_claim_only_after_session_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveAttemptProbe()
            claims = AttemptClaims.live()
            session = Session(store)
            transport = Transport(*lifecycle_replies())
            controller = live_controller(api(transport, scope().api_origin), store, claims, session,
                                           delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            confirmed = await controller.run(record())
            self.assertIsInstance(confirmed, DeliveryAcceptedCheckpoint)
            self.assertEqual(store.records, ())
            self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
            released = claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())
            released.release(owner=released.lifecycle_owner)
            self.assertIsNone(await controller.settle())
            with self.assertRaises(ControllerRefused):
                await controller.run(record())
            self.assertEqual(len(transport.calls), 8)

    async def test_terminal_start_replays_retire_the_allocated_session_without_entering_science(self):
        for state in ("succeeded", "failed", "expired"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                store = LiveAttemptProbe()
                claims = AttemptClaims.live()
                session = Session(store)
                transport = Transport(reply(start_response(state=state, replayed=True)))
                controller = live_controller(api(transport, scope().api_origin), store, claims, session,
                                               delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
                terminal = await controller.run(record())
                self.assertIsInstance(terminal, AcknowledgedAttempt)
                self.assertEqual(terminal.admission.state, state)
                self.assertEqual(store.records, ())
                self.assertEqual([event[0] for event in session.events], ["retire"])
                self.assertEqual(len(transport.calls), 1)
                released = claims.claim_live(record(), lifecycle_owner=LiveAttemptProbe())
                released.release(owner=released.lifecycle_owner)
