"""Retained results publish through the shared proxy after producer exit."""

import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.provider_proxy import ServicePolicy, ServiceProxyServer
from provider_core.shared_delivery_ipc import SharedDeliverySocket
from provider_core.shared_delivery_service import SharedDeliveryService
from provider_core.shared_delivery_config import SharedDeliveryConfiguration
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.attempts import fail_response
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class SharedDeliveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reload_shortens_existing_backoff_and_preserves_exact_body(self):
        item = outcome()

        async def response(_request, _body):
            if len(peer.requests) == 1:
                peer.response_status = 429
                return b"retry"
            peer.response_status = 200
            return fail_response(execution_attempt_ref=item.execution_attempt_ref,
                                 failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE")

        async with api_tls_peer("success", response_factory=response) as peer:
            item = replace(item, api_origin=peer.origin)
            ca = (Path(peer.scratch.name) / "cert.pem").read_bytes()
            credential = encoded(document(principal_ref="provider:test"))
            fields = {"origin": peer.origin, "profile": "dev-local", "topology": "dev-local",
                      "provider_ref": "provider:test", "exchange_seconds": 2, "retirement_seconds": 1,
                      "credential_file": "/gone/key", "ca_file": "/gone/ca"}
            _, _, material = api_settings_with_material(
                fields, lambda _path, **options: credential if options["label"] == "API signing credential" else ca
            )
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "state").mkdir(mode=0o700)
                (root / "ipc").mkdir(mode=0o700)
                config = root / "settings.toml"

                def settings(delay):
                    return ('[outbox]\nprovider_items = 4\nprovider_bytes = 16384\ntotal_items = 8\n'
                            f'total_bytes = 32768\nretry_initial_seconds = {delay}\nretry_max_seconds = {delay}\nlog_level = "INFO"\n')

                config.write_text(settings(60))
                limits = QueueLimits(4, 16384, 8, 32768)
                queue = DeliveryQueue(root / "state", limits=limits)
                queue.enqueue_handoff(DeliveryHandoff(item, material))
                queue.close()
                policy = ServicePolicy.admit("mapped", [urlsplit(peer.origin).netloc], ["127.0.0.1=127.0.0.1"])
                with ServiceProxyServer(root / "proxy.sock", "lo", policy, status_directory=root) as proxy:
                    thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.01})
                    thread.start()
                    retry, published = asyncio.Event(), asyncio.Event()

                    class Notices:
                        def set_level(self, _level):
                            pass

                        def __call__(self, _error, facts):
                            if facts["event"] == "delivery_retry":
                                retry.set()
                            if facts["event"] == "delivery_published":
                                published.set()

                    service = SharedDeliveryService(root / "state", limits=limits,
                        socket=SharedDeliverySocket(root / "ipc/http.sock", None), proxy_socket=root / "proxy.sock",
                        request_seconds=2, retry_initial_seconds=60, retry_max_seconds=60,
                        on_notice=Notices(), diagnostic_status=Mock(), live_configuration=SharedDeliveryConfiguration(config))
                    task = asyncio.create_task(service.run())
                    try:
                        await asyncio.wait_for(retry.wait(), 5)
                        replacement = root / "replacement.toml"
                        replacement.write_text(settings(0.01))
                        replacement.replace(config)
                        service.next_reload = 0
                        service._reload()
                        await asyncio.wait_for(published.wait(), 3)
                        self.assertEqual(peer.request_bodies, [item.body, item.body])
                    finally:
                        service.request_stop()
                        await asyncio.wait_for(task, 5)
                        await asyncio.to_thread(proxy.shutdown)
                        await asyncio.to_thread(thread.join, 2)

    async def test_restart_uses_each_rows_retained_key_and_ca_through_proxy(self):
        items = [outcome(), outcome(suffix="1")]

        async def response(_request, _body):
            item = items[len(peer.requests) - 1]
            return fail_response(execution_attempt_ref=item.execution_attempt_ref,
                                 failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE")

        async with api_tls_peer("success", response_factory=response) as peer:
            items = [replace(item, api_origin=peer.origin) for item in items]
            ca = (Path(peer.scratch.name) / "cert.pem").read_bytes()
            materials, keys = [], []
            for _ in items:
                credential = encoded(document(principal_ref="provider:test"))
                fields = {"origin": peer.origin, "profile": "dev-local", "topology": "dev-local",
                          "provider_ref": "provider:test", "exchange_seconds": 2, "retirement_seconds": 1,
                          "credential_file": "/gone/key", "ca_file": "/gone/ca"}
                settings, _, material = api_settings_with_material(
                    fields, lambda _path, **options: credential if options["label"] == "API signing credential" else ca
                )
                materials.append(material)
                keys.append(settings.identity.private_key)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "state").mkdir(mode=0o700)
                (root / "ipc").mkdir(mode=0o700)
                limits = QueueLimits(4, 16384, 8, 32768)
                queue = DeliveryQueue(root / "state", limits=limits)
                for item, material in zip(items, materials):
                    queue.enqueue_handoff(DeliveryHandoff(item, material))
                queue.close()
                authority = urlsplit(peer.origin).netloc
                policy = ServicePolicy.admit("mapped", [authority], ["127.0.0.1=127.0.0.1"])
                with ServiceProxyServer(root / "proxy.sock", "lo", policy, status_directory=root) as proxy:
                    thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.01})
                    thread.start()
                    published = asyncio.Event()
                    notices = []

                    def notice(error, facts):
                        notices.append((error, facts))
                        if sum(event[1]["event"] == "delivery_published" for event in notices) == 2:
                            published.set()

                    service = SharedDeliveryService(root / "state", limits=limits,
                        socket=SharedDeliverySocket(root / "ipc/http.sock", None), proxy_socket=root / "proxy.sock",
                        request_seconds=2, retry_initial_seconds=0.01, retry_max_seconds=0.02,
                        on_notice=notice, diagnostic_status=Mock())
                    task = asyncio.create_task(service.run())
                    try:
                        await asyncio.wait_for(published.wait(), 5)
                        for request, item, key in zip(peer.requests, items, keys):
                            verify_signed_request(request, item.body, origin=peer.origin, key=key)
                        self.assertEqual(peer.request_bodies, [item.body for item in items])
                        self.assertEqual(service.queue.inventory(peer.origin, "provider:test"), [])
                    finally:
                        service.request_stop()
                        await asyncio.wait_for(task, 5)
                        await asyncio.to_thread(proxy.shutdown)
                        await asyncio.to_thread(thread.join, 2)
                    self.assertTrue(service.retirement_confirmed)
