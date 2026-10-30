"""The common socket accepts handoffs without exposing queued work."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryRejected
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.delivery_store import DeliveryConflict, DeliveryQueue, QueueLimits
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
