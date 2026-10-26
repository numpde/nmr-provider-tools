"""Pending bytes reach the API without a producer, model or scientific callback."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryQueue, DeliveryStorageFailed, QueueLimits
from provider_core.delivery_worker import DeliveryWorker, DeliveryWorkerFailed
from provider_core.failure_projection import owned_details
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.api import response_headers, signature_window_problem
from tests.fakes.attempts import fail_response
from tests.fakes.delivery import outcome
from tests.fakes.api import api


class DeliveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        (self.root / "state").mkdir(mode=0o700)
        self.queue = DeliveryQueue(self.root / "state", limits=QueueLimits(4, 16384, 8, 32768))
        self.addCleanup(self.queue.close)
        self.inbox = DeliveryInbox(self.queue, (), request_seconds=1, on_accepted=Mock(), on_failure=Mock(), binding_status=Mock())
        self.addAsyncCleanup(self.inbox.close)
        self.notices = []

    # silent-defaults: allow-contract -- The fixture stops at publication unless a fault scenario chooses a different observed event.
    def worker(self, peer, *, stop_on="delivery_published"):
        client = api(HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.2), peer.origin)
        endpoint = DeliveryEndpoint(peer.origin, "provider:test", self.root / "delivery.sock")

        def notice(error, facts):
            self.notices.append((error, facts))
            if facts["event"] == stop_on:
                worker.request_stop()

        worker = DeliveryWorker(
            self.inbox, endpoint, client, retry_initial_seconds=0.01, retry_max_seconds=0.02, on_notice=notice
        )
        return worker

    async def test_matching_signed_api_receipt_removes_payload_without_a_completed_archive(self):
        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
        )) as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            worker = self.worker(peer)
            await asyncio.wait_for(worker.run(), 2)
            self.assertEqual(peer.request_bodies, [item.body])
            verify_signed_request(peer.requests[0], item.body, origin=peer.origin, key=worker.api.identity.private_key)
            self.assertEqual(self.queue.inventory(item.api_origin, item.provider_ref), [])
            self.assertNotIn(b"SCIENTIFIC_PAYLOAD_NEEDLE", (self.root / "state/delivery.sqlite3").read_bytes())
            self.assertTrue(worker.api.retirement_confirmed)

    async def test_lost_api_reply_keeps_exact_bytes_for_freshly_signed_replay(self):
        async with api_tls_peer("success", response_bodies=[None, fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE", replayed=True
        )]) as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            worker = self.worker(peer)
            await asyncio.wait_for(worker.run(), 2)
            self.assertEqual(peer.request_bodies, [item.body, item.body])
            for request in peer.requests:
                verify_signed_request(request, item.body, origin=peer.origin, key=worker.api.identity.private_key)
            signatures = [request.split(b"Signature-Input: ")[1].split(b"\r\n")[0] for request in peer.requests]
            self.assertNotEqual(signatures[0], signatures[1])
            self.assertEqual([facts["event"] for _, facts in self.notices], ["delivery_retry", "delivery_published"])
            self.assertEqual(self.queue.inventory(item.api_origin, item.provider_ref), [])

    async def test_api_rate_limit_and_authentication_refusals_recover_without_restart(self):
        async def response(_request, _body):
            if len(peer.requests) == 1:
                peer.response_status = 429
                peer.response_headers = ((b"content-type", b"text/html"),)
                return b"rate limited"
            if len(peer.requests) == 2:
                peer.response_status = 401
                peer.response_headers = response_headers(status=401, request_id=b"request:test")
                return signature_window_problem(code="authentication_failed")
            peer.response_status = 200
            peer.response_headers = response_headers()
            return fail_response(execution_attempt_ref=outcome().execution_attempt_ref,
                                 failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE")

        async with api_tls_peer("success", response_factory=response) as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            worker = self.worker(peer)
            await asyncio.wait_for(worker.run(), 2)
            self.assertEqual(peer.request_bodies, [item.body] * 3)
            for request in peer.requests:
                verify_signed_request(request, item.body, origin=peer.origin, key=worker.api.identity.private_key)
            signatures = [request.split(b"Signature-Input: ")[1].split(b"\r\n")[0] for request in peer.requests]
            self.assertEqual(len(set(signatures)), 3)
            self.assertEqual([error.status for error, facts in self.notices if facts["event"] == "delivery_retry"], [429, 401])
            self.assertFalse(worker.paused)
            self.assertEqual(self.queue.inventory(item.api_origin, item.provider_ref), [])

    async def test_retry_after_above_backoff_cap_preserves_other_binding_progress_and_prompt_stop(self):
        retry_seen = asyncio.Event()
        async with api_tls_peer("success", response_status=429, response_body=b"rate limited",
                                response_headers=((b"content-type", b"text/html"), (b"retry-after", b"120"))) as limited:
            async with api_tls_peer("success", response_body=fail_response(
                execution_attempt_ref=outcome().execution_attempt_ref,
                failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
            )) as healthy:
                first, second = replace(outcome(), api_origin=limited.origin), replace(outcome(), api_origin=healthy.origin)
                self.queue.enqueue(first)
                self.queue.enqueue(second)
                worker = self.worker(limited)
                original_notice = worker.on_notice

                def notice(error, facts):
                    original_notice(error, facts)
                    if facts["event"] == "delivery_retry":
                        retry_seen.set()

                worker.on_notice = notice
                original = asyncio.create_task(worker.run())
                try:
                    await asyncio.wait_for(retry_seen.wait(), 1)
                    self.assertGreater(self.notices[0][1]["retry_seconds"], 120)
                    self.assertLessEqual(self.notices[0][1]["retry_seconds"], 120 + worker.retry_max_seconds)
                    await asyncio.wait_for(self.worker(healthy).run(), 1)
                    self.assertFalse(original.done())
                    self.assertEqual(limited.request_bodies, [first.body])
                    self.assertEqual(self.queue.inventory(second.api_origin, second.provider_ref), [])
                finally:
                    worker.request_stop()
                    await asyncio.wait_for(original, 1)
                self.assertFalse(worker.paused)
                self.assertEqual(self.queue.next_pending(first.api_origin, first.provider_ref), first)

    async def test_unmatched_receipt_holds_one_item_and_next_item_can_publish(self):
        second = outcome(suffix="1")
        async with api_tls_peer("success", response_bodies=[fail_response(failure_message="WRONG_RECEIPT"), fail_response(
            execution_attempt_ref=second.execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
        )]) as peer:
            first, second = replace(outcome(), api_origin=peer.origin), replace(second, api_origin=peer.origin)
            self.queue.enqueue(first)
            self.queue.enqueue(second)
            worker = self.worker(peer)
            await asyncio.wait_for(worker.run(), 2)
            self.assertEqual(peer.request_bodies, [first.body, second.body])
            retained = self.queue.inventory(first.api_origin, first.provider_ref)
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0]["execution_attempt_ref"], first.execution_attempt_ref)
            self.assertEqual(retained[0]["state"], "held")
            self.assertEqual([facts["event"] for _, facts in self.notices], ["delivery_held", "delivery_published"])

    async def test_restart_publishes_committed_bytes_without_producer_state(self):
        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE", replayed=True
        )) as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            self.queue.close()
            self.queue = DeliveryQueue(self.root / "state", limits=QueueLimits(4, 16384, 8, 32768))
            self.addCleanup(self.queue.close)
            self.inbox.queue = self.queue
            await asyncio.wait_for(self.worker(peer).run(), 2)
            self.assertEqual(peer.request_bodies, [item.body])
            self.assertEqual(self.queue.inventory(item.api_origin, item.provider_ref), [])

    async def test_topology_failure_pauses_binding_without_holding_its_pending_items(self):
        # silent-defaults: allow-contract -- Change only the topology header to exercise the wrong-environment refusal.
        headers = [(name, b"web" if name.lower() == b"nmr-api-topology" else value)
                   for name, value in response_headers()]
        async with api_tls_peer("success", response_headers=headers) as peer:
            first, second = replace(outcome(), api_origin=peer.origin), replace(outcome(suffix="1"), api_origin=peer.origin)
            self.queue.enqueue(first)
            self.queue.enqueue(second)
            worker = self.worker(peer)
            await asyncio.wait_for(worker.run(), 2)
            self.assertTrue(worker.paused)
            self.assertEqual(peer.request_bodies, [first.body])
            self.assertEqual([row["state"] for row in self.queue.inventory(first.api_origin, first.provider_ref)], ["pending", "pending"])
            self.assertEqual(self.notices[0][1]["event"], "delivery_binding_paused")

    async def test_another_binding_publishes_while_first_api_reply_is_still_pending(self):
        release = asyncio.Event()

        async def delayed_response(request, body):
            await release.wait()
            return fail_response(execution_attempt_ref=outcome().execution_attempt_ref,
                                 failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE")

        async with api_tls_peer("success", response_factory=delayed_response) as slow:
            async with api_tls_peer("success", response_body=fail_response(
                execution_attempt_ref=outcome().execution_attempt_ref,
                failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
            )) as healthy:
                first, second = replace(outcome(), api_origin=slow.origin), replace(outcome(), api_origin=healthy.origin)
                self.queue.enqueue(first)
                self.queue.enqueue(second)
                slow_worker = self.worker(slow)
                original = asyncio.create_task(slow_worker.run())
                try:
                    await asyncio.wait_for(slow.received.wait(), 1)
                    await asyncio.wait_for(self.worker(healthy).run(), 1)
                    self.assertFalse(original.done())
                    self.assertEqual(self.queue.next_pending(first.api_origin, first.provider_ref), first)
                    self.assertEqual(self.queue.inventory(second.api_origin, second.provider_ref), [])
                finally:
                    release.set()
                    await asyncio.wait_for(original, 2)

    async def test_cancelled_send_preserves_original_queue_and_uncertain_api_effect(self):
        async with api_tls_peer("headers-stall") as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            worker = self.worker(peer)
            original = asyncio.create_task(worker.run())
            await asyncio.wait_for(peer.received.wait(), 1)
            original.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await original
            self.assertEqual(peer.request_bodies, [item.body])
            self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)
            self.assertTrue(worker.api.retirement_confirmed)
            self.assertTrue(worker.failure.api_failure.attempt_mutation_may_have_committed)
            self.assertFalse(owned_details(worker.failure)["api_receipt_received"])

    async def test_failed_deletion_retains_received_receipt_fact_and_does_not_resend(self):
        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
        )) as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            worker = self.worker(peer)
            failure = DeliveryStorageFailed("Synthetic storage write refusal.")
            with patch.object(self.queue, "remove_published", side_effect=failure):
                with self.assertRaises(DeliveryWorkerFailed) as stopped:
                    await asyncio.wait_for(worker.run(), 2)
            self.assertIs(stopped.exception.cause, failure)
            self.assertTrue(owned_details(stopped.exception)["api_receipt_received"])
            self.assertEqual(peer.request_bodies, [item.body])
            self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)

    async def test_later_queue_read_does_not_inherit_previous_items_receipt(self):
        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
        )) as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            worker = self.worker(peer, stop_on="delivery_worker_failed")
            failure = DeliveryStorageFailed("Synthetic next-item read refusal.")
            with patch.object(self.queue, "next_pending", side_effect=[item, failure]):
                with self.assertRaises(DeliveryWorkerFailed) as stopped:
                    await asyncio.wait_for(worker.run(), 2)
            self.assertIs(stopped.exception.cause, failure)
            self.assertFalse(owned_details(stopped.exception)["api_receipt_received"])
            self.assertEqual(peer.request_bodies, [item.body])
            self.assertEqual(self.queue.inventory(item.api_origin, item.provider_ref), [])

    async def test_cancellation_during_final_retirement_keeps_cancellation_and_cleanup_evidence(self):
        async with api_tls_peer("success") as peer:
            worker = self.worker(peer)
            worker.request_stop()
            entered = asyncio.Event()
            retire = worker.api.retire

            async def delayed_retirement(*, owner):
                await retire(owner=owner)
                entered.set()
                await asyncio.Event().wait()

            with patch.object(worker.api, "retire", side_effect=delayed_retirement):
                original = asyncio.create_task(worker.run())
                await asyncio.wait_for(entered.wait(), 1)
                original.cancel()
                with self.assertRaises(asyncio.CancelledError) as cancelled:
                    await original
            self.assertIs(cancelled.exception.__cause__, worker.failure)
            self.assertIsInstance(worker.failure.cleanup_failures[0], asyncio.CancelledError)
            self.assertEqual(peer.requests, [])
