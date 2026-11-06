"""Operator notices describe the same pending report through publication retries."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock

from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.delivery_worker import DeliveryWorker
from provider_core.transport import HttpTransport
from tests.fakes.api import api, response_headers
from tests.fakes.api_tls import api_tls_peer, verify_signed_request
from tests.fakes.attempts import fail_response
from tests.fakes.delivery import outcome


class DeliveryProgressNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / "state").mkdir(mode=0o700)
        self.queue = DeliveryQueue(self.root / "state", limits=QueueLimits(4, 16384, 8, 32768))
        self.addCleanup(self.queue.close)
        self.inbox = DeliveryInbox(self.queue, (), request_seconds=1, on_accepted=Mock(),
                                   on_failure=Mock(), binding_status=Mock())
        self.addAsyncCleanup(self.inbox.close)
        self.notices = []

    def worker(self, peer, *, trust, stop_events, restore_after_retries):
        client = api(HttpTransport(trust=trust, exchange_seconds=0.2, retirement_seconds=0.2), peer.origin)
        endpoint = DeliveryEndpoint(peer.origin, "provider:test", self.root / "unused-relay.sock")

        def notice(error, facts):
            self.notices.append((error, facts, asyncio.get_running_loop().time()))
            retries = sum(item[1]["event"] == "delivery_retry" for item in self.notices)
            if retries == 2:
                restore_after_retries.set()
            if facts["event"] in stop_events:
                worker.request_stop()

        worker = DeliveryWorker(self.inbox, endpoint, client, retry_initial_seconds=0.04,
                                retry_max_seconds=0.08, on_notice=notice)
        return worker

    def evidence(self):
        records = []
        for error, facts, at in self.notices:
            reason = None
            if error is not None:
                reason = type(error).__name__
            records.append({"error": reason, "at": at, **facts})
        return json.dumps(records, sort_keys=True)

    @staticmethod
    def unavailable(peer):
        peer.response_status = 503
        peer.response_headers = response_headers(status=503, request_id=b"request:b4-publication-outage")
        return json.dumps({
            "type": "urn:nmr-api:problem:service-unavailable", "title": "Service unavailable", "status": 503,
            "instance": "urn:nmr-api:request:b4-publication-outage",
            "request_id": "request:b4-publication-outage", "code": "service_unavailable",
        }).encode()

    async def test_retry_warnings_report_elapsed_wait_and_matching_receipt_reports_recovery(self):
        loop = asyncio.get_running_loop()
        restored = asyncio.Event()
        request_times, request_owners = [], []

        def response_mode(_request, _body):
            request_times.append(loop.time())
            request_owners.append(worker.api.pending_owner is worker)
            # The first genuine HTTPS request exhausts its existing exchange
            # budget. The second receives a retryable API refusal.
            if len(peer.requests) == 1:
                return "headers-stall"
            return "success"

        async def response(_request, _body):
            if not restored.is_set():
                return self.unavailable(peer)
            peer.response_status, peer.response_headers = 200, response_headers()
            return fail_response(execution_attempt_ref=item.execution_attempt_ref,
                                 failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE")

        async with api_tls_peer("success", response_factory=response, response_mode=response_mode) as peer:
            item = replace(outcome(), api_origin=peer.origin)
            self.queue.enqueue(item)
            # Queue age and this publisher's elapsed wait are different facts.
            # A notice must not include time before this owner began publishing.
            await asyncio.sleep(0.12)
            worker = self.worker(peer, trust=peer.trust, stop_events={"delivery_published"},
                                 restore_after_retries=restored)
            worker_started = loop.time()
            await asyncio.wait_for(worker.run(), 3)
            evidence = self.evidence()
            self.assertEqual(["delivery_retry", "delivery_retry", "delivery_published"],
                             [facts["event"] for _, facts, _ in self.notices], msg=evidence)
            self.assertEqual([item.body] * 3, peer.request_bodies, msg=evidence)
            self.assertEqual([True] * 3, request_owners, msg=evidence)
            self.assertIsNone(self.notices[0][0].status, msg=evidence)
            self.assertEqual(503, self.notices[1][0].status, msg=evidence)
            self.assertIsNone(self.notices[2][0], msg=evidence)
            for request in peer.requests:
                verify_signed_request(request, item.body, origin=peer.origin, key=worker.api.identity.private_key)
            self.assertEqual([], self.queue.inventory(item.api_origin, item.provider_ref), msg=evidence)
            self.assertTrue(worker.api.retirement_confirmed, msg=evidence)

            waits = []
            for _error, facts, observed_at in self.notices:
                for name, value in item.acceptance().items():
                    self.assertEqual(value, facts[name], msg=evidence)
                self.assertIn("publication_wait_seconds", facts,
                              msg="The operator cannot tell how long this exact report has lacked a publication receipt: " + evidence)
                age = facts["publication_wait_seconds"]
                self.assertIsInstance(age, (int, float), msg=evidence)
                # The publication call starts after worker startup and before
                # the TLS peer receives its first request. A small scheduling
                # allowance covers calculating a fact before its callback.
                self.assertGreaterEqual(age + 0.02, observed_at - request_times[0], msg=evidence)
                self.assertLessEqual(age, observed_at - worker_started + 0.02, msg=evidence)
                waits.append(age)
            self.assertLess(waits[0], waits[1], msg=evidence)
            self.assertLess(waits[1], waits[2], msg=evidence)
            self.assertIs(self.notices[-1][1]["recovered"], True, msg=evidence)

    async def test_mismatched_receipt_keeps_its_alert_when_a_different_report_publishes(self):
        restored = asyncio.Event()
        second = outcome(suffix="1")

        async def response(_request, body):
            if len(peer.requests) == 1:
                return self.unavailable(peer)
            peer.response_status, peer.response_headers = 200, response_headers()
            if body == first.body:
                return fail_response(execution_attempt_ref=first.execution_attempt_ref,
                                     failure_code="provider_interrupted", failure_message="WRONG_RECEIPT")
            return fail_response(execution_attempt_ref=second.execution_attempt_ref,
                                 failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE")

        async with api_tls_peer("success", response_factory=response) as peer:
            first, second = replace(outcome(), api_origin=peer.origin), replace(second, api_origin=peer.origin)
            self.queue.enqueue(first)
            self.queue.enqueue(second)
            worker = self.worker(peer, trust=peer.trust, stop_events={"delivery_published"},
                                 restore_after_retries=restored)
            await asyncio.wait_for(worker.run(), 3)
            evidence = self.evidence()
            self.assertEqual(["delivery_retry", "delivery_held", "delivery_published"],
                             [facts["event"] for _, facts, _ in self.notices], msg=evidence)
            self.assertEqual([first.body, first.body, second.body], peer.request_bodies, msg=evidence)
            retained = self.queue.inventory(first.api_origin, first.provider_ref)
            self.assertEqual([(first.execution_attempt_ref, "held")],
                             [(row["execution_attempt_ref"], row["state"]) for row in retained], msg=evidence)
            for _, facts, _ in self.notices:
                if facts["execution_attempt_ref"] == first.execution_attempt_ref:
                    self.assertTrue("recovered" not in facts or facts["recovered"] is False, msg=evidence)
            published = self.notices[-1][1]
            self.assertEqual(second.execution_attempt_ref, published["execution_attempt_ref"], msg=evidence)
            self.assertIn("recovered", published, msg=evidence)
            self.assertIs(published["recovered"], False, msg=evidence)

    async def test_tls_trust_failure_keeps_pending_report_and_never_reports_recovery(self):
        loop = asyncio.get_running_loop()
        previous_handler, peer_failures = loop.get_exception_handler(), []

        def observe_peer_failure(_loop, context):
            peer_failures.append(context)

        # Client-side certificate rejection can reset the test server's TLS
        # handshake before its callback starts. Retain and check those reports.
        loop.set_exception_handler(observe_peer_failure)
        try:
            async with api_tls_peer("success") as peer, api_tls_peer("success") as other_trust:
                item = replace(outcome(), api_origin=peer.origin)
                self.queue.enqueue(item)
                worker = self.worker(peer, trust=other_trust.trust, stop_events=set(),
                                     restore_after_retries=asyncio.Event())
                await asyncio.wait_for(worker.run(), 3)
                evidence = self.evidence()
                self.assertTrue(worker.paused, msg=evidence)
                self.assertEqual(["delivery_binding_paused"], [facts["event"] for _, facts, _ in self.notices], msg=evidence)
                self.assertEqual([], peer.requests, msg=evidence)
                self.assertEqual(item, self.queue.next_pending(item.api_origin, item.provider_ref), msg=evidence)
                self.assertEqual(item.execution_attempt_ref, self.notices[0][1]["execution_attempt_ref"], msg=evidence)
                self.assertTrue("recovered" not in self.notices[0][1] or self.notices[0][1]["recovered"] is False, msg=evidence)
        finally:
            loop.set_exception_handler(previous_handler)
        for context in peer_failures:
            self.assertIsInstance(context["exception"], (ConnectionResetError, ssl.SSLError))
