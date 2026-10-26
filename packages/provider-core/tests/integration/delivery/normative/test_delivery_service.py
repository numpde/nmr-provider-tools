"""A local producer hands off once; the shared service alone publishes it."""

import asyncio
import errno
import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryUnavailable
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_logging import DeliveryNotices
from provider_core.delivery_service import DeliveryBinding, DeliveryService
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.api import response_headers
from tests.fakes.attempts import fail_response
from tests.fakes.delivery import healthy_diagnostics, outcome
from tests.fakes.api import api


class DeliveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        (self.root / "state").mkdir(mode=0o700)
        (self.root / "ipc").mkdir(mode=0o700)
        self.limits = QueueLimits(4, 16384, 8, 32768)
        self.notices = []
        self.published = asyncio.Event()
        self.notice_changed = asyncio.Event()

    def service(self, peer):
        endpoint = DeliveryEndpoint(peer.origin, "provider:test", self.root / "ipc/http.sock")
        client = api(HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.2), peer.origin)

        def notice(error, facts):
            self.notices.append((error, facts))
            if facts["event"] != "delivery_ready":
                self.notice_changed.set()
            if facts["event"] == "delivery_published":
                self.published.set()

        return DeliveryService(
            self.root / "state", limits=self.limits, bindings=(DeliveryBinding(endpoint, client),),
            request_seconds=1, retry_initial_seconds=0.01, retry_max_seconds=0.02, on_notice=notice, diagnostic_status=healthy_diagnostics,
        ), endpoint

    async def test_producer_acknowledgement_and_api_receipt_have_distinct_connected_effects(self):
        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
        )) as peer:
            service, endpoint = self.service(peer)
            original = asyncio.create_task(service.run())
            client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
            try:
                await asyncio.wait_for(service.ready.wait(), 1)
                item = replace(outcome(), api_origin=peer.origin)
                self.assertEqual(await client.submit(item), item.acceptance())
                await asyncio.wait_for(self.published.wait(), 2)
                self.assertEqual(peer.request_bodies, [item.body])
            finally:
                service.request_stop()
                await asyncio.wait_for(original, 2)
                await client.retire()
            self.assertTrue(service.retirement_confirmed)
            self.assertFalse(endpoint.socket_path.exists())
            reopened = DeliveryQueue(self.root / "state", limits=self.limits)
            try:
                self.assertEqual(reopened.inventory(peer.origin, endpoint.provider_ref), [])
            finally:
                reopened.close()

    async def test_interrupted_shutdown_keeps_original_commit_owned_until_later_settlement(self):
        async with api_tls_peer("success") as peer:
            service, endpoint = self.service(peer)
            original = asyncio.create_task(service.run())
            await asyncio.wait_for(service.ready.wait(), 1)
            client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
            item = replace(outcome(), api_origin=peer.origin)
            started, release = threading.Event(), threading.Event()
            enqueue = service.queue.enqueue

            def delayed_enqueue(value):
                started.set()
                if not release.wait(2):
                    raise TimeoutError("Test did not release the original delivery commit.")
                return enqueue(value)

            service.queue.enqueue = delayed_enqueue
            submit = asyncio.create_task(client.submit(item))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            service.request_stop()
            async with asyncio.timeout(1):
                while service._close_task is None:
                    await asyncio.sleep(0)
            original.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await original
            closing = asyncio.create_task(service.close())
            try:
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.assertFalse(service.retirement_confirmed)
            finally:
                release.set()
            self.assertEqual(await submit, item.acceptance())
            await asyncio.wait_for(closing, 2)
            await client.retire()
            self.assertTrue(service.retirement_confirmed)
            self.assertEqual(peer.requests, [])
            reopened = DeliveryQueue(self.root / "state", limits=self.limits)
            try:
                self.assertEqual(reopened.next_pending(item.api_origin, item.provider_ref), item)
            finally:
                reopened.close()

    async def test_scoped_status_reports_paused_publisher_while_inbox_still_accepts(self):
        # silent-defaults: allow-contract -- Change only the topology header to exercise the wrong-environment refusal.
        headers = [(name, b"web" if name.lower() == b"nmr-api-topology" else value)
                   for name, value in response_headers()]
        async with api_tls_peer("success", response_headers=headers) as peer:
            service, endpoint = self.service(peer)
            original = asyncio.create_task(service.run())
            client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
            try:
                await asyncio.wait_for(service.ready.wait(), 1)
                await client.submit(replace(outcome(), api_origin=peer.origin))
                await asyncio.wait_for(self.notice_changed.wait(), 1)
                pending = await client.pending()
                self.assertEqual(pending["publisher"]["state"], "paused")
                self.assertIn("restart", pending["publisher"]["message"])
                self.assertEqual(pending["items"][0]["state"], "pending")
                self.assertNotIn("SCIENTIFIC_PAYLOAD_NEEDLE", json.dumps(pending))
            finally:
                service.request_stop()
                await asyncio.wait_for(original, 2)
                await client.retire()

    async def test_provider_allowances_must_fit_host_budget_before_storage_opens(self):
        async with api_tls_peer("success") as peer:
            service, endpoint = self.service(peer)
            other = DeliveryBinding(
                DeliveryEndpoint(peer.origin, "provider:other", self.root / "other/http.sock"),
                api(HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.2), peer.origin),
            )
            with self.assertRaisesRegex(ValueError, "host delivery budget"):
                DeliveryService(
                    self.root / "unused", limits=QueueLimits(4, 16384, 4, 16384),
                    bindings=(*service.bindings, other), request_seconds=1,
                    retry_initial_seconds=0.01, retry_max_seconds=0.02, on_notice=lambda error, facts: None, diagnostic_status=healthy_diagnostics,
                )
            self.assertFalse((self.root / "unused").exists())
            self.assertEqual(peer.requests, [])

    async def test_stop_during_startup_joins_original_listener_without_starting_delivery(self):
        async with api_tls_peer("success") as peer:
            service, endpoint = self.service(peer)
            entered, release = asyncio.Event(), asyncio.Event()
            start = DeliveryInbox.start

            async def delayed_start(inbox):
                entered.set()
                await release.wait()
                await start(inbox)

            with patch.object(DeliveryInbox, "start", delayed_start):
                original = asyncio.create_task(service.run())
                await asyncio.wait_for(entered.wait(), 1)
                item = replace(outcome(), api_origin=peer.origin)
                service.queue.enqueue(item)
                closing = asyncio.create_task(service.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.assertFalse(service.retirement_confirmed)
                release.set()
                await asyncio.wait_for(closing, 2)
                await asyncio.wait_for(original, 2)
            self.assertTrue(service.retirement_confirmed)
            self.assertFalse(service.ready.is_set())
            self.assertFalse(endpoint.socket_path.exists())
            self.assertEqual(peer.requests, [])

    async def test_changed_binding_refuses_startup_without_retargeting_original_pending_bytes(self):
        item = outcome()
        previous = DeliveryQueue(self.root / "state", limits=self.limits)
        previous.enqueue(item)
        previous.close()
        async with api_tls_peer("success") as peer:
            service, endpoint = self.service(peer)
            with self.assertRaisesRegex(DeliveryUnavailable, "bindings missing"):
                await service.run()
            self.assertFalse(service.ready.is_set())
            self.assertFalse(endpoint.socket_path.exists())
            self.assertEqual(peer.requests, [])
            self.assertEqual(self.notices[0][1], {
                "event": "delivery_binding_missing", "api_origin": item.api_origin, "provider_ref": item.provider_ref,
            })
        reopened = DeliveryQueue(self.root / "state", limits=self.limits)
        try:
            self.assertEqual(reopened.next_pending(item.api_origin, item.provider_ref), item)
        finally:
            reopened.close()

    async def test_failed_log_output_does_not_reverse_publication_and_is_visible_in_status(self):
        class BrokenStream:
            def write(self, content):
                raise BrokenPipeError(errno.EPIPE, "PRIVATE_SINK_DETAIL")

            def flush(self):
                pass

        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
        )) as peer:
            notices = DeliveryNotices(BrokenStream(), secrets=())
            self.addCleanup(notices.close)
            service, endpoint = self.service(peer)
            service.on_notice, service.diagnostic_status = notices, notices.status
            original = asyncio.create_task(service.run())
            client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
            try:
                await asyncio.wait_for(service.ready.wait(), 1)
                item = replace(outcome(), api_origin=peer.origin)
                await client.submit(item)
                async with asyncio.timeout(2):
                    while True:
                        pending = await client.pending()
                        diagnostics = pending["publisher"]["diagnostics"]
                        if diagnostics["last_failed_event"] == "delivery_published":
                            break
                        await asyncio.sleep(0.01)
                self.assertEqual(pending["items"], [])
                self.assertEqual(peer.request_bodies, [item.body])
                self.assertEqual(diagnostics["last_failure_errno"], errno.EPIPE)
                self.assertNotIn("PRIVATE", json.dumps(pending))
            finally:
                service.request_stop()
                await asyncio.wait_for(original, 2)
                await client.retire()
            self.assertTrue(service.retirement_confirmed)

    async def test_failed_second_binding_identifies_its_grant_and_retires_first_listener(self):
        async with api_tls_peer("success") as peer:
            template, first = self.service(peer)
            bad_parent = self.root / "bad-grant"
            bad_parent.mkdir(mode=0o755)
            bad_parent.chmod(0o755)
            second = DeliveryEndpoint(peer.origin, "provider:other", bad_parent / "http.sock")
            other_api = api(HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.2), peer.origin)
            output = io.StringIO()
            notices = DeliveryNotices(output, secrets=())
            self.addCleanup(notices.close)
            service = DeliveryService(
                self.root / "state", limits=self.limits, bindings=(*template.bindings, DeliveryBinding(second, other_api)),
                request_seconds=1, retry_initial_seconds=0.01, retry_max_seconds=0.02,
                on_notice=notices, diagnostic_status=notices.status,
            )
            with self.assertRaises(DeliveryUnavailable):
                await service.run()
            report = json.loads(output.getvalue())
            self.assertEqual(report["event"], "delivery_binding_start_failed")
            self.assertEqual(report["provider_ref"], second.provider_ref)
            self.assertEqual(report["api_origin"], peer.origin)
            self.assertEqual(report["socket_path"], str(second.socket_path))
            self.assertIn("mode 0755", report["failure"]["message"])
            self.assertFalse(first.socket_path.exists())
            self.assertFalse(second.socket_path.exists())
            self.assertEqual(peer.requests, [])
            self.assertTrue(service.retirement_confirmed)
