"""Unexpected publisher cancellation cannot silently strand accepted work."""

import asyncio
import tempfile
import threading
import unittest
import pathlib
from dataclasses import replace
from unittest.mock import Mock
from urllib.parse import urlsplit

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.provider_proxy import ServicePolicy, ServiceProxyServer
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_delivery_ipc import SharedDeliverySocket
from provider_core.shared_delivery_service import SharedDeliveryService
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class SharedServiceCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_publisher_fails_service_and_retains_the_queued_handoff(self):
        async with api_tls_peer("headers-stall") as peer:
            delivery_outcome = replace(outcome(), api_origin=peer.origin)
            ca = (pathlib.Path(peer.scratch.name) / "cert.pem").read_bytes()
            credential = encoded(document(principal_ref=delivery_outcome.provider_ref))

            fields = {
                "origin": peer.origin,
                "profile": "dev-local",
                "topology": "dev-local",
                "provider_ref": delivery_outcome.provider_ref,
                "exchange_seconds": 10,
                "retirement_seconds": 1,
                "credential_file": "/fixture/credential",
                "ca_file": "/fixture/ca"
            }

            startup_file_contents = {fields["credential_file"]: credential, fields["ca_file"]: ca}

            (settings, _, material) = api_settings_with_material(
                fields,
                lambda startup_file_path, **_file_read_options: startup_file_contents[startup_file_path]
            )

            handoff = DeliveryHandoff(delivery_outcome, material)

            with tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)

                (root / "state").mkdir(mode=0o700)
                (root / "ipc").mkdir(mode=0o700)
                limits = QueueLimits(2, 8192, 4, 16384)
                policy = ServicePolicy.admit("mapped", [urlsplit(peer.origin).netloc], ["127.0.0.1=127.0.0.1"])

                with ServiceProxyServer(root / "proxy.sock", "lo", policy, status_directory=root) as proxy:
                    thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.01})

                    service = SharedDeliveryService(
                        root / "state",
                        limits=limits,
                        socket=SharedDeliverySocket(root / "ipc/socket", None),
                        proxy_socket=root / "proxy.sock",
                        ipc_io_timeout_seconds=2,
                        retry_initial_seconds=0.01,
                        retry_max_seconds=0.02,
                        on_notice=Mock(),
                        diagnostic_status=Mock()
                    )

                    thread.start()
                    task = asyncio.create_task(service.run())
                    client = None

                    try:
                        await asyncio.wait_for(service.ready.wait(), 5)
                        endpoint = DeliveryEndpoint(peer.origin, delivery_outcome.provider_ref, root / "ipc/socket")
                        client = SharedDeliveryClient(endpoint, material, exchange_seconds=2, retirement_seconds=1)

                        self.assertEqual(await client.submit(delivery_outcome), handoff.acceptance())
                        await asyncio.wait_for(peer.received.wait(), 5)
                        worker = service.workers[delivery_outcome.key[:2]]
                        api = worker.api

                        service._worker_tasks[delivery_outcome.key[:2]].cancel()

                        with self.assertRaisesRegex(RuntimeError, "worker was cancelled"):
                            await asyncio.wait_for(task, 5)

                        self.assertTrue(api.retirement_confirmed)
                        self.assertTrue(service.retirement_confirmed)
                        self.assertEqual(peer.request_bodies, [delivery_outcome.body])

                        verify_signed_request(
                            peer.requests[0],
                            delivery_outcome.body,
                            origin=peer.origin,
                            key=settings.identity.private_key
                        )
                    finally:
                        try:
                            if client is not None:
                                await client.retire()
                        finally:
                            try:
                                if not task.done():
                                    service.request_stop()

                                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
                            finally:
                                await asyncio.to_thread(proxy.shutdown)
                                await asyncio.to_thread(thread.join, 2)

                queue = DeliveryQueue(root / "state", limits=limits)

                try:
                    retained = queue.next_pending_handoff(delivery_outcome.api_origin, delivery_outcome.provider_ref)

                    self.assertEqual(retained.outcome, delivery_outcome)
                    self.assertEqual(retained.publication_bytes(), handoff.publication_bytes())
                finally:
                    queue.close()
