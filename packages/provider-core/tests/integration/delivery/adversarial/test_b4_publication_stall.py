"""A verified API receipt does not hide a publication blocked in local storage."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.delivery_worker import DeliveryWorker
from provider_core.transport import HttpTransport
from tests.fakes.api import api
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.attempts import fail_response
from tests.fakes.delivery import outcome


class PublicationStorageStallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        (self.root / "state").mkdir(mode=0o700)
        self.queue = DeliveryQueue(self.root / "state", limits=QueueLimits(4, 16384, 8, 32768))
        self.addCleanup(self.queue.close)
        self.inbox = DeliveryInbox(self.queue, (), request_seconds=1, on_accepted=Mock(),
                                   on_failure=Mock(), binding_status=Mock())
        self.addAsyncCleanup(self.inbox.close)

    async def test_original_receipt_waiting_for_storage_emits_one_alert_and_recovers_without_resending(self):
        loop = asyncio.get_running_loop()
        removal_started, warning_seen = asyncio.Event(), asyncio.Event()
        release_removal = Event()
        removal_calls, notices = [], []
        original_remove = self.queue.remove_published

        def held_remove(item):
            # DeliveryInbox still owns this exact offloaded operation. Returning
            # the matching API receipt is what authorizes entering this method.
            removal_calls.append(item)
            loop.call_soon_threadsafe(removal_started.set)
            if not release_removal.wait(5):
                raise AssertionError("The test did not release its original storage operation")
            return original_remove(item)

        def notice(failure, facts):
            notices.append((failure, dict(facts), loop.time()))
            if facts["event"] == "delivery_stalled":
                warning_seen.set()
            if facts["event"] == "delivery_published":
                worker.request_stop()

        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE",
        )) as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            client = api(HttpTransport(trust=peer.trust, exchange_seconds=0.2, retirement_seconds=0.2), peer.origin)
            endpoint = DeliveryEndpoint(peer.origin, item.provider_ref, self.root / "unused-relay.sock")
            worker = DeliveryWorker(self.inbox, endpoint, client, retry_initial_seconds=0.04,
                                    retry_max_seconds=0.08, on_notice=notice)
            # Existing public grants define when an uninterrupted publication
            # wait becomes an operator concern; no new test-only policy exists.
            alert_seconds = client.exchange_seconds + client.retirement_seconds + worker.retry_max_seconds
            started_at = loop.time()
            with patch.object(self.queue, "remove_published", side_effect=held_remove):
                original = asyncio.create_task(worker.run())
                try:
                    await asyncio.wait_for(removal_started.wait(), 3)
                    try:
                        await asyncio.wait_for(warning_seen.wait(), alert_seconds + 0.25)
                    except TimeoutError:
                        # Assert the missing warning with the pre-teardown
                        # operation/receipt facts, rather than a timeout error.
                        pass
                    waiting = self.queue.next_pending(item.api_origin, item.provider_ref)
                    evidence = json.dumps({
                        "alert_seconds": alert_seconds,
                        "observed_wait_seconds": loop.time() - started_at,
                        "api_receipt_received": worker._api_receipt_received,
                        "api_retirement_confirmed": client.retirement_confirmed,
                        "original_storage_call_count": len(removal_calls),
                        "storage_released": release_removal.is_set(),
                        "original_report_retained": waiting == item,
                        "request_count": len(peer.requests),
                        "notices": [{"error": None if failure is None else type(failure).__name__, **facts}
                                    for failure, facts, _ in notices],
                    }, sort_keys=True)
                    self.assertFalse(original.done(), msg=evidence)
                    self.assertFalse(release_removal.is_set(), msg=evidence)
                    self.assertEqual([item], removal_calls, msg=evidence)
                    self.assertEqual(item, waiting, msg=evidence)
                    self.assertEqual([item.body], peer.request_bodies, msg=evidence)
                    self.assertTrue(worker._api_receipt_received, msg=evidence)
                    self.assertTrue(client.retirement_confirmed, msg=evidence)
                    self.assertEqual(["delivery_stalled"], [facts["event"] for _, facts, _ in notices],
                                     msg="No operator warning described the original publication blocked in storage: " + evidence)
                    failure, stalled, observed_at = notices[0]
                    self.assertIsNotNone(failure, msg="DeliveryNotices must emit this event at warning severity")
                    for name, value in item.acceptance().items():
                        self.assertEqual(value, stalled[name], msg=evidence)
                    self.assertIs(stalled["api_receipt_received"], True, msg=evidence)
                    self.assertGreaterEqual(stalled["publication_wait_seconds"], alert_seconds, msg=evidence)
                    self.assertLessEqual(stalled["publication_wait_seconds"], observed_at - started_at + 0.02,
                                         msg=evidence)

                    # The original owner remains held; a second elapsed budget
                    # must neither repeat the alert nor resend its accepted report.
                    await asyncio.sleep(alert_seconds + 0.02)
                    self.assertEqual(1, len(notices), msg=evidence)
                    self.assertEqual([item], removal_calls, msg=evidence)
                    self.assertEqual([item.body], peer.request_bodies, msg=evidence)
                    release_removal.set()
                    await asyncio.wait_for(asyncio.shield(original), 3)
                finally:
                    release_removal.set()
                    worker.request_stop()
                    await asyncio.wait_for(asyncio.shield(original), 3)

            self.assertEqual(["delivery_stalled", "delivery_published"],
                             [facts["event"] for _, facts, _ in notices])
            failure, published, _ = notices[-1]
            self.assertIsNone(failure)
            self.assertIs(published["recovered"], True)
            self.assertGreater(published["publication_wait_seconds"], stalled["publication_wait_seconds"])
            for name, value in item.acceptance().items():
                self.assertEqual(value, published[name])
            self.assertEqual([item], removal_calls)
            self.assertEqual([item.body], peer.request_bodies)
            verify_signed_request(peer.requests[0], item.body, origin=peer.origin, key=client.identity.private_key)
            self.assertEqual([], self.queue.inventory(item.api_origin, item.provider_ref))
            self.assertTrue(client.retirement_confirmed)
