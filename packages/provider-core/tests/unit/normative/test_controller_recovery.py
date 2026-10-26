"""The controller retains one relay handoff across outages without new science."""

import asyncio
import tempfile
import unittest
from unittest.mock import Mock, patch

from provider_core.controller import AttemptController
from provider_core.delivery_client import DeliveryUnavailable
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from tests.fakes.api import Transport, api
from tests.fakes.attempt_store import record, scope
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.controller import SingleRetirementSession, empty_store, lifecycle_replies


class ControllerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_outage_beyond_thirty_seconds_preserves_exact_handoff_without_new_science(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session, delivery = SingleRetirementSession(store), RecordingDelivery()
            attempted, submit = [], delivery.submit
            failure = DeliveryUnavailable("Relay unavailable", status=503)
            async def interrupted(item):
                attempted.append(item.body)
                if len(attempted) == 1:
                    raise failure
                return await submit(item)
            delivery.submit = interrupted
            delivery.can_retry = Mock(return_value=True)
            loop = asyncio.get_running_loop()
            real_time, elapsed = loop.time, 0
            def retry(*_):
                nonlocal elapsed
                elapsed += 31
            transport = Transport(*lifecycle_replies())
            controller = AttemptController(api(transport, scope().api_origin), store, AttemptClaims(), session,
                delivery=delivery, on_delivery_retry=retry, delivery_retry_initial_seconds=0.001,
                delivery_retry_max_seconds=0.002, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                with patch.object(loop, "time", side_effect=lambda: real_time() + elapsed):
                    accepted = await controller.run(record())
                self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
                self.assertEqual(elapsed, 31)
                self.assertEqual(attempted, [delivery.items[0].body] * 2)
                self.assertEqual(len(transport.calls), 8)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(await store.inventory(), ())
            finally:
                await store.close()

    async def test_same_controller_retries_owned_bytes_then_clears_after_relay_ack_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session, delivery = SingleRetirementSession(store), RecordingDelivery()
            attempted, submit = [], delivery.submit
            failures = [DeliveryUnavailable("Unconfirmed relay reply"), DeliveryUnavailable("Relay unavailable", status=503)]
            async def interrupted(item):
                attempted.append(item.body)
                if failures:
                    raise failures.pop(0)
                return await submit(item)
            delivery.submit = interrupted
            delivery.can_retry = Mock(return_value=True)
            retry = Mock()
            transport = Transport(*lifecycle_replies())
            controller = AttemptController(api(transport, scope().api_origin), store, AttemptClaims(), session,
                delivery=delivery, on_delivery_retry=retry, delivery_retry_initial_seconds=0.001,
                delivery_retry_max_seconds=0.002, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            try:
                accepted = await controller.run(record())
                self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
                self.assertEqual(attempted, [delivery.items[0].body] * 3)
                self.assertEqual(accepted.selection.body_sha256, delivery.items[0].body_sha256)
                self.assertEqual(retry.call_count, 2)
                self.assertEqual(retry.call_args.args[0].start, record())
                self.assertEqual(len(transport.calls), 8)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(await store.inventory(), ())
                self.assertIsNone(await controller.settle())
            finally:
                await store.close()
