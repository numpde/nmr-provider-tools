"""Proposed, unaccepted policy: progress past a refused credential.

This probe is outside the maintained test lane; it assumes later independent
Attempts may be published before an earlier unresolved Attempt.
"""

import asyncio
import tempfile
import sys
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
from tests.fakes.api import response_headers, signature_window_problem
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.attempts import fail_response
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class AuthorizationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_original_credential_does_not_block_later_credential(self):
        """Policy assumption: independent Attempts may progress without discarding an older refusal.

        The API never accepts the first credential in this scenario; a 401 alone
        does not establish revocation. Both attempts belong to one provider.
        """
        items = [outcome(suffix="0"), outcome(suffix="1")]
        keys = []
        bad_signatures = []

        async def reply(request, body):
            index = next((i for i, item in enumerate(items) if body == item.body), None)
            if index is None:
                bad_signatures.append("unexpected terminal body")
                index = 0
            try:
                verify_signed_request(request, body, origin=peer.origin, key=keys[index])
            except Exception:
                bad_signatures.append("request signed with the wrong retained key")
            if index == 0 or bad_signatures:
                peer.response_status = 401
                peer.response_headers = response_headers(status=401, request_id=b"request:test")
                return signature_window_problem(code="authentication_failed")
            peer.response_status = 200
            peer.response_headers = response_headers()
            return fail_response(execution_attempt_ref=items[1].execution_attempt_ref,
                                 failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE")

        async with api_tls_peer("success", response_factory=reply) as peer:
            items = [replace(item, api_origin=peer.origin) for item in items]
            ca = (Path(peer.scratch.name) / "cert.pem").read_bytes()
            materials = []
            for index in range(2):
                credential = encoded(document(principal_ref="provider:test", credential_ref=f"credential:test-{index}"))
                fields = {"origin": peer.origin, "profile": "dev-local", "topology": "dev-local",
                          "provider_ref": "provider:test", "exchange_seconds": 2, "retirement_seconds": 1,
                          "credential_file": "/gone/key", "ca_file": "/gone/ca"}
                settings, _, material = api_settings_with_material(
                    fields, lambda _path, **options: credential if options["label"] == "API signing credential" else ca)
                keys.append(settings.identity.private_key)
                materials.append(material)

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "state").mkdir(mode=0o700)
                (root / "ipc").mkdir(mode=0o700)
                limits = QueueLimits(4, 16384, 8, 32768)
                queue = DeliveryQueue(root / "state", limits=limits)
                for item, material in zip(items, materials):
                    queue.enqueue_handoff(DeliveryHandoff(item, material))
                queue.close()
                policy = ServicePolicy.admit("mapped", [urlsplit(peer.origin).netloc], ["127.0.0.1=127.0.0.1"])
                with ServiceProxyServer(root / "proxy.sock", "lo", policy, status_directory=root) as proxy:
                    thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.01})
                    thread.start()
                    published = asyncio.Event()

                    def notice(_error, facts):
                        if facts["event"] == "delivery_published" and facts["execution_attempt_ref"] == items[1].execution_attempt_ref:
                            published.set()

                    service = SharedDeliveryService(root / "state", limits=limits,
                        socket=SharedDeliverySocket(root / "ipc/http.sock", None), proxy_socket=root / "proxy.sock",
                        request_seconds=2, retry_initial_seconds=0.01, retry_max_seconds=0.02,
                        on_notice=notice, diagnostic_status=Mock())
                    task = asyncio.create_task(service.run())
                    try:
                        try:
                            await asyncio.wait_for(published.wait(), 2)
                        # silent-defaults: allow-contract -- Continue to inspect the retained outcome before asserting publication.
                        except TimeoutError:
                            pass
                        self.assertIn(items[0].body, peer.request_bodies, "The API never refused the first request")
                        self.assertEqual(bad_signatures, [], "A terminal request changed its retained signing identity")
                        inventory = service.queue.inventory(peer.origin, "provider:test")
                        retained = [row for row in inventory if row["execution_attempt_ref"] == items[0].execution_attempt_ref]
                        self.assertEqual(len(retained), 1, "The refused first handoff must remain recoverable")
                        self.assertIn(retained[0]["state"], ("pending", "held"))
                        self.assertEqual(retained[0]["body_sha256"], items[0].body_sha256)
                        original = DeliveryHandoff(items[0], materials[0])
                        self.assertEqual(service.queue.enqueue_handoff(original), original.acceptance())
                        self.assertTrue(published.is_set(),
                            "A repeatedly refused credential stranded the later independent outcome for the same provider")
                    finally:
                        primary = sys.exception()
                        cleanup_failures = []
                        service.request_stop()
                        try:
                            await asyncio.wait_for(asyncio.shield(task), 5)
                        except BaseException as error:
                            cleanup_failures.append(error)
                        try:
                            await asyncio.to_thread(proxy.shutdown)
                        except BaseException as error:
                            cleanup_failures.append(error)
                        try:
                            await asyncio.to_thread(thread.join, 2)
                            if thread.is_alive():
                                raise RuntimeError("Test proxy thread retirement remains unconfirmed")
                        except BaseException as error:
                            cleanup_failures.append(error)
                        if cleanup_failures:
                            if primary is not None:
                                cleanup_failures.insert(0, primary)
                            raise BaseExceptionGroup("Test operation or owner retirement failed", cleanup_failures)


if __name__ == "__main__":
    unittest.main()
