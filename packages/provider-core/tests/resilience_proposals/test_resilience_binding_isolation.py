"""Proposed, unaccepted policy: isolate a settled publisher failure.

The injected generic exception may indicate shared failure. This probe is not
part of the maintained test lane; its cleanup still needs bounded refinement.
"""

import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_client import DeliveryEndpoint, DeliveryUnavailable
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.provider_proxy import ServicePolicy, ServiceProxyServer
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.shared_delivery_ipc import SharedDeliverySocket
from provider_core.shared_delivery_service import SharedDeliveryService
from provider_core.shared_delivery_worker import SharedDeliveryWorker
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import fail_response
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class BindingIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_settled_binding_failure_preserves_unrelated_submission_and_publication(self):
        first = outcome(provider="provider:first", suffix="1")
        second = outcome(provider="provider:second", suffix="2")

        async def response(_request, _body):
            return fail_response(execution_attempt_ref=second.execution_attempt_ref,
                                 failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE")

        async with api_tls_peer("success", response_factory=response) as peer:
            first = replace(first, api_origin=peer.origin)
            second = replace(second, api_origin=peer.origin)
            ca = (Path(peer.scratch.name) / "cert.pem").read_bytes()
            materials = []
            for item in (first, second):
                credential = encoded(document(principal_ref=item.provider_ref))
                fields = {"origin": peer.origin, "profile": "dev-local", "topology": "dev-local",
                          "provider_ref": item.provider_ref, "exchange_seconds": 2, "retirement_seconds": 1,
                          "credential_file": "/fixture/credential", "ca_file": "/fixture/ca"}
                _, _, material = api_settings_with_material(
                    fields, lambda _path, **options: credential if options["label"] == "API signing credential" else ca)
                materials.append(material)

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "state").mkdir(mode=0o700)
                (root / "ipc").mkdir(mode=0o700)
                limits = QueueLimits(2, 8192, 4, 16384)
                policy = ServicePolicy.admit("mapped", [urlsplit(peer.origin).netloc], ["127.0.0.1=127.0.0.1"])
                with ServiceProxyServer(root / "proxy.sock", "lo", policy, status_directory=root) as proxy:
                    thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.01})
                    thread.start()
                    failed, published = asyncio.Event(), asyncio.Event()
                    first_api, first_task = [], []
                    original_deliver = SharedDeliveryWorker._deliver

                    async def fail_first(worker, item):
                        if item.provider_ref == first.provider_ref:
                            first_api.append(worker.api)
                            first_task.append(asyncio.current_task())
                            raise RuntimeError("injected publisher failure")
                        return await original_deliver(worker, item)

                    def notice(_error, facts):
                        if facts["event"] == "delivery_worker_failed" and facts.get("provider_ref") == first.provider_ref:
                            failed.set()
                        if facts["event"] == "delivery_published" and facts.get("provider_ref") == second.provider_ref:
                            published.set()

                    service = SharedDeliveryService(
                        root / "state", limits=limits, socket=SharedDeliverySocket(root / "ipc/socket", None),
                        proxy_socket=root / "proxy.sock", request_seconds=2, retry_initial_seconds=0.01,
                        retry_max_seconds=0.02, on_notice=notice, diagnostic_status=Mock())
                    clients = []
                    with patch.object(SharedDeliveryWorker, "_deliver", fail_first):
                        task = asyncio.create_task(service.run())
                        try:
                            await asyncio.wait_for(service.ready.wait(), 5)
                            for item, material in zip((first, second), materials):
                                clients.append(SharedDeliveryClient(
                                    DeliveryEndpoint(peer.origin, item.provider_ref, root / "ipc/socket"), material,
                                    exchange_seconds=2, retirement_seconds=1))
                            self.assertEqual(await clients[0].submit(first), DeliveryHandoff(first, materials[0]).acceptance())
                            await asyncio.wait_for(failed.wait(), 5)
                            await asyncio.gather(first_task[0], return_exceptions=True)
                            self.assertTrue(first_api[0].retirement_confirmed)
                            try:
                                accepted = await clients[1].submit(second)
                            except DeliveryUnavailable as cause:
                                self.fail(f"An unrelated provider cannot submit after the first publisher settled: {type(cause).__name__}")
                            self.assertEqual(accepted, DeliveryHandoff(second, materials[1]).acceptance())
                            try:
                                await asyncio.wait_for(published.wait(), 3)
                            except TimeoutError:
                                self.fail("An unrelated provider was accepted but not published after the first publisher settled")
                            self.assertEqual(peer.request_bodies, [second.body])
                        finally:
                            for client in clients:
                                await client.retire()
                            service.request_stop()
                            await asyncio.gather(task, return_exceptions=True)
                            await asyncio.to_thread(proxy.shutdown)
                            await asyncio.to_thread(thread.join, 2)
                            queue = DeliveryQueue(root / "state", limits=limits)
                            try:
                                status = queue.status(first.api_origin, first.provider_ref, first.execution_attempt_ref)
                                self.assertIsNotNone(status)
                                self.assertIn(status["state"], ("pending", "held"))
                                self.assertEqual(status["body_sha256"], first.body_sha256)
                                handoff = DeliveryHandoff(first, materials[0])
                                self.assertEqual(queue.enqueue_handoff(handoff), handoff.acceptance())
                                self.assertEqual(queue.status(first.api_origin, first.provider_ref, first.execution_attempt_ref)["state"], status["state"])
                            finally:
                                queue.close()
                queue = DeliveryQueue(root / "state", limits=limits)
                try:
                    self.assertIsNone(queue.next_pending(second.api_origin, second.provider_ref))
                finally:
                    queue.close()
