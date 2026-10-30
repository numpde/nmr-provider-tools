"""The common socket accepts handoffs without exposing queued work."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryRejected, DeliveryUnavailable
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.delivery_store import DeliveryConflict, DeliveryFull, DeliveryQueue, QueueLimits
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_delivery_ipc import SharedDeliveryInbox, SharedDeliverySocket
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class SharedDeliveryIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        root = Path(self.scratch.name)
        (root / "ipc").mkdir(mode=0o700)
        (root / "state").mkdir(mode=0o700)
        self.queue = DeliveryQueue(root / "state", limits=QueueLimits(2, 8192, 4, 16384))
        self.addCleanup(self.queue.close)
        self.socket = root / "ipc/http.sock"
        self.inbox = SharedDeliveryInbox(self.queue, SharedDeliverySocket(self.socket, None), request_seconds=1,
                                         on_accepted=Mock(), on_failure=Mock())
        await self.inbox.start()
        self.addAsyncCleanup(self.inbox.close)

    def client(self, provider):
        fields = {"origin": "https://api.example", "profile": "dev-local", "topology": "dev-local",
                  "provider_ref": provider, "exchange_seconds": 10, "retirement_seconds": 5,
                  "credential_file": "/private/credential"}
        _, _, material = api_settings_with_material(fields, Mock(return_value=encoded(document(principal_ref=provider))))
        endpoint = DeliveryEndpoint("https://api.example", provider, self.socket)
        client = SharedDeliveryClient(endpoint, material, exchange_seconds=1, retirement_seconds=0.2)
        self.addAsyncCleanup(client.retire)
        return client

    async def test_two_providers_can_submit_but_neither_can_read_the_queue(self):
        first, second = self.client("provider:first"), self.client("provider:second")
        item1, item2 = outcome(provider="provider:first"), outcome(provider="provider:second")
        self.assertEqual(await first.submit(item1), DeliveryHandoff(item1, first.publication).acceptance())
        self.assertEqual(await second.submit(item2), DeliveryHandoff(item2, second.publication).acceptance())
        reader = DeliveryClient(first.endpoint, exchange_seconds=1, retirement_seconds=0.2)
        self.addAsyncCleanup(reader.retire)
        with self.assertRaises(DeliveryRejected):
            await reader.pending()
        with self.assertRaises(DeliveryRejected):
            await reader.status(item2.execution_attempt_ref)
        self.assertEqual(self.queue.next_pending_handoff(item1.api_origin, item1.provider_ref).outcome, item1)
        self.assertEqual(self.queue.next_pending_handoff(item2.api_origin, item2.provider_ref).outcome, item2)
        another = self.client("provider:first")
        with self.assertRaises(DeliveryConflict):
            await another.submit(item1)

    async def test_failed_publisher_wake_preserves_acknowledged_commit_and_fails_service(self):
        client = self.client("provider:first")
        item = outcome(provider="provider:first")
        self.inbox.on_accepted = Mock(side_effect=OSError("wake failed"))
        self.assertEqual(await client.submit(item), DeliveryHandoff(item, client.publication).acceptance())
        self.assertTrue(self.inbox.failed.is_set())
        self.assertIsInstance(self.inbox.failure, OSError)
        self.assertEqual(self.queue.next_pending_handoff(item.api_origin, item.provider_ref).outcome, item)

    async def test_lost_ack_replays_the_same_retained_result_and_material(self):
        client = self.client("provider:first")
        item = outcome(provider="provider:first")
        expected = DeliveryHandoff(item, client.publication)
        respond = self.inbox._respond
        dropped = False

        async def lose_first(connection, writer, status, document):
            nonlocal dropped
            if not dropped and status == 200 and document == expected.acceptance():
                dropped = True
                writer.close()
                await writer.wait_closed()
                return
            await respond(connection, writer, status, document)

        self.inbox._respond = lose_first
        with self.assertRaises(DeliveryUnavailable):
            await client.submit(item)
        await client.retire()
        self.assertEqual(expected.acceptance(), await client.submit(item))
        retained = self.queue.next_pending_handoff(item.api_origin, item.provider_ref)
        self.assertEqual(item, retained.outcome)
        self.assertEqual(client.publication, retained.publication)
        self.assertEqual(1, len(self.queue.inventory(item.api_origin, item.provider_ref)))

    async def test_full_queue_refuses_new_handoff_but_accepts_identical_replay(self):
        client = self.client("provider:first")
        first, second, third = (outcome(provider="provider:first", suffix=suffix) for suffix in ("0", "1", "2"))
        accepted = await client.submit(first)
        await client.submit(second)
        with self.assertRaises(DeliveryFull):
            await client.submit(third)
        self.assertEqual(accepted, await client.submit(first))
        self.assertEqual(2, len(self.queue.inventory(first.api_origin, first.provider_ref)))
