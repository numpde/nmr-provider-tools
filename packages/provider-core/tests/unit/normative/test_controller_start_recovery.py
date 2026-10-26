"""A lost START reply reuses its retained command before science is entered."""

from provider_core.producer_checkpoint import AcknowledgedCheckpoint
from tests.fakes.checkpoint_execution import RecordingDelivery
from provider_core.producer_checkpoint import AcknowledgedCheckpoint
import asyncio
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpcore

from provider_core.attempt_record import AcknowledgedAttempt
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.controller import AttemptController, StartRecovery
from provider_core.ownership import AttemptClaims
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import start_response
from tests.fakes.controller import SingleRetirementSession, empty_store, lifecycle_replies
from tests.fakes.api import api


class ControllerStartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_start_replay_has_fresh_authentication_and_one_scientific_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            pending = record()
            transport = Transport(
                ExchangeFailed("Unverified reply", primary=httpcore.RemoteProtocolError("PRIVATE_WIRE")),
                reply(b"maintenance", status=503),
                reply(start_response(replayed=True)),
                *lifecycle_replies()[1:],
            )
            retry, recovered = Mock(), Mock()
            loop = asyncio.get_running_loop()
            clock, elapsed = loop.time, 0

            def outage_wait(*_):
                nonlocal elapsed
                self.assertEqual(session.events, [])
                elapsed += 31

            retry.side_effect = outage_wait
            claims = AttemptClaims()
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                claims,
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                start_recovery=StartRecovery(0.002, retry, recovered),
            )
            try:
                with patch.object(loop, "time", side_effect=lambda: clock() + elapsed):
                    confirmed = await controller.run(pending)
                self.assertIsInstance(confirmed, DeliveryAcceptedCheckpoint)
                self.assertEqual(await store.inventory(), ())
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                # The actual prepare callback observes a durable replayed acknowledgement.
                self.assertIsInstance(session.events[0][1], AcknowledgedCheckpoint)
                self.assertTrue(session.events[0][1].admission.replayed)
                starts = [request for request, _ in transport.calls[:3]]
                self.assertEqual([request.content for request in starts], [pending.body] * 3)
                self.assertEqual(len({request.headers["signature-input"] for request in starts}), 3)
                self.assertEqual(len({request.headers["signature"] for request in starts}), 3)
                self.assertEqual(len(transport.calls), 10)
                self.assertEqual(retry.call_count, 2)
                self.assertIs(retry.call_args.args[0], pending)
                recovered.assert_called_once()
                self.assertEqual(recovered.call_args.args[0].admission, session.events[0][1].admission)
                self.assertIs(recovered.call_args.args[1], retry.call_args_list[0].args[1])
                self.assertIsNone(await controller.settle())
                released = claims.claim_start(pending)
                released.release()
            finally:
                await store.close()

    async def test_terminal_start_replay_retires_session_without_preparing_or_executing(self):
        for state in ("succeeded", "failed", "expired"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                store = await empty_store(directory, maximum_records=1)
                session = SingleRetirementSession(store)
                pending = record()
                transport = Transport(reply(b"lost reply", status=500), reply(start_response(state=state, replayed=True)))
                recovered = Mock()
                controller = AttemptController(
                    api(transport, scope().api_origin),
                    store,
                    AttemptClaims(),
                    session,
                    delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                    start_recovery=StartRecovery(0.001, Mock(), recovered),
                )
                try:
                    acknowledged = await controller.run(pending)
                    self.assertEqual(acknowledged.admission.state, state)
                    self.assertEqual(await store.inventory(), ())
                    self.assertEqual([event[0] for event in session.events], ["retire"])
                    self.assertEqual([request.content for request, _ in transport.calls], [pending.body] * 2)
                    recovered.assert_called_once()
                    self.assertIsNone(await controller.settle())
                finally:
                    await store.close()

    async def test_admitted_start_replay_may_finish_bounded_io_after_retry_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            transport = Transport(reply(b"maintenance", status=503), reply(start_response(state="expired", replayed=True)))
            exchange = transport.exchange

            async def delayed_receipt(request, *, maximum_bytes, owner=None):
                response = await exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                if len(transport.calls) == 2:
                    await asyncio.sleep(0.15)
                return response

            transport.exchange = delayed_receipt
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                delivery=RecordingDelivery(), on_delivery_retry=Mock(), delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04, allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                start_recovery=StartRecovery(0.001, Mock(), Mock()),
            )
            try:
                self.assertEqual((await controller.run(record())).admission.state, "expired")
                self.assertEqual(len(transport.calls), 2)
                self.assertEqual([event[0] for event in session.events], ["retire"])
            finally:
                await store.close()
