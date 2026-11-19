"""A private producer receives acknowledgement only for its durable exact body."""

import asyncio
import json
import tempfile
import threading
import unittest
import os
import pathlib
from unittest.mock import AsyncMock, Mock

import h11

from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryRejected, DeliveryUnavailable
from provider_core.delivery_ipc import DeliveryInbox, DeliveryRequestRejected
from provider_core.delivery_store import MAX_PROVIDER_ITEMS, DeliveryFull, DeliveryOutcome, DeliveryQueue, QueueLimits
from provider_core.attempt_record import TerminalOperation
from provider_core.adapters.nmr.v1.attempt_request import fail_body
from provider_core.failure_projection import owned_details
from tests.fakes.delivery import healthy_diagnostics, outcome


class DeliveryIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()

        self.addCleanup(self.scratch.cleanup)
        root = pathlib.Path(self.scratch.name)

        (root / "ipc").mkdir(mode=0o700)
        (root / "state").mkdir(mode=0o700)
        self.queue = DeliveryQueue(root / "state", limits=QueueLimits(1, 4096, 2, 8192))

        self.addCleanup(self.queue.close)
        self.endpoint = DeliveryEndpoint("https://api.example", "provider:test", root / "ipc/http.sock")

        (self.accepted, self.failures) = (Mock(), Mock())

        self.inbox = DeliveryInbox(
            self.queue,
            (self.endpoint,),
            request_seconds=1,
            on_accepted=self.accepted,
            on_failure=self.failures,
            binding_status=Mock(return_value={"state": "inbox_only", "message": "No API publisher is attached to this test inbox.", "diagnostics": healthy_diagnostics()}),
        )

        await self.inbox.start()
        self.addAsyncCleanup(self.inbox.close)
        self.client = DeliveryClient(self.endpoint, exchange_seconds=1, retirement_seconds=0.2)

        self.addAsyncCleanup(self.client.retire)

    async def test_submit_and_inventory_prove_durability(self):
        delivery_outcome = outcome()
        accepted = await self.client.submit(delivery_outcome)

        self.assertEqual(accepted, delivery_outcome.acceptance())
        self.assertEqual(self.queue.next_pending(delivery_outcome.api_origin, delivery_outcome.provider_ref), delivery_outcome)
        pending = await self.client.pending()

        self.assertEqual(pending["items"][0]["body_sha256"], delivery_outcome.body_sha256)
        self.assertEqual(await self.client.status(delivery_outcome.execution_attempt_ref), pending["items"][0])
        self.assertEqual(await self.client.submit(delivery_outcome), accepted)
        self.assertEqual(len(self.queue.inventory(delivery_outcome.api_origin, delivery_outcome.provider_ref)), 1)
        self.assertTrue(self.client.retirement_confirmed)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_inventory_omits_payload(self):
        await self.client.submit(outcome())

        self.assertNotIn("SCIENTIFIC_PAYLOAD_NEEDLE", json.dumps(await self.client.pending()))

    async def test_full_queue_refuses_new_outcome_but_allows_identical_retry(self):
        delivery_outcome = outcome()

        await self.client.submit(delivery_outcome)

        with self.assertRaises(DeliveryFull):
            await self.client.submit(outcome(suffix="1"))

        self.assertEqual(await self.client.submit(delivery_outcome), delivery_outcome.acceptance())
        self.assertEqual(self.queue.next_pending(delivery_outcome.api_origin, delivery_outcome.provider_ref), delivery_outcome)

    async def test_maximum_supported_inventory_remains_readable_with_escaped_held_reasons(self):
        self.queue.limits = QueueLimits(MAX_PROVIDER_ITEMS, 8 * 1024 * 1024, MAX_PROVIDER_ITEMS, 8 * 1024 * 1024)
        self.inbox.request_seconds = 5
        client = DeliveryClient(self.endpoint, exchange_seconds=5, retirement_seconds=0.2)

        self.addAsyncCleanup(client.retire)
        expected = []

        for index in range(MAX_PROVIDER_ITEMS):
            reference = "execution_attempt:sha256:" + f"{index:064x}"

            delivery_outcome = DeliveryOutcome(
                self.endpoint.api_origin,
                self.endpoint.provider_ref,
                "job:" + "j" * 124,
                reference,
                TerminalOperation.FAIL,
                fail_body(reference, "provider_interrupted", "fixture")
            )

            self.queue.enqueue(delivery_outcome)
            self.queue.hold(delivery_outcome, "🧪" * 512)
            expected.append(reference)

        pending = await client.pending()

        self.assertEqual(set(expected), {row["execution_attempt_ref"] for row in pending["items"]})
        self.assertTrue(all(row["held_reason"] == ("🧪" * 512) for row in pending["items"]))

        with self.assertRaises(DeliveryFull):
            await client.submit(outcome(suffix="f"))

        self.assertEqual(MAX_PROVIDER_ITEMS, len((await client.pending())["items"]))

    async def test_oversized_legacy_inventory_refuses_only_listing_and_keeps_status_and_publication_available(self):
        expected = []

        for index in range(MAX_PROVIDER_ITEMS + 1):
            reference = "execution_attempt:sha256:" + f"{index:064x}"

            delivery_outcome = DeliveryOutcome(
                self.endpoint.api_origin,
                self.endpoint.provider_ref,
                "job:legacy",
                reference,
                TerminalOperation.FAIL,
                fail_body(reference, "provider_interrupted", "retained exact body")
            )

            self.queue._db.execute(
                "INSERT INTO outstanding VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    delivery_outcome.api_origin,
                    delivery_outcome.provider_ref,
                    delivery_outcome.execution_attempt_ref,
                    delivery_outcome.job_ref,
                    delivery_outcome.operation.value,
                    delivery_outcome.body,
                    delivery_outcome.body_sha256,
                    "pending",
                    1.0,
                    None
                ),
            )

            expected.append(delivery_outcome)

        self.queue._db.commit()
        before = self.queue._db.execute("SELECT * FROM outstanding ORDER BY attempt_ref").fetchall()

        with self.assertRaises(DeliveryUnavailable):
            await self.client.pending()

        await asyncio.sleep(0)
        self.assertFalse(self.inbox.failed.is_set())
        self.assertIsNone(self.inbox.failure)
        self.assertEqual(expected[-1].body_sha256, (await self.client.status(expected[-1].execution_attempt_ref))["body_sha256"])
        self.assertEqual(before, self.queue._db.execute("SELECT * FROM outstanding ORDER BY attempt_ref").fetchall())
        published = self.queue.next_pending(self.endpoint.api_origin, self.endpoint.provider_ref)

        self.assertEqual(expected[0], published)
        self.queue.remove_published(published)
        self.assertEqual(MAX_PROVIDER_ITEMS, len((await self.client.pending())["items"]))
        self.assertFalse(self.inbox.failed.is_set())

    async def test_display_diagnostic_changes_do_not_block_ownership_or_capacity_reads(self):
        delivery_outcome = outcome()

        await self.client.submit(delivery_outcome)

        self.inbox.binding_status = lambda endpoint: {
            "state": "inbox_only",
            "diagnostics": "display format changed",
            "new_diagnostic": {"count": 2},
        }

        pending = await self.client.pending()

        self.assertEqual(pending["items"][0]["body_sha256"], delivery_outcome.body_sha256)
        self.assertEqual(pending["capacity"]["provider_items"], 1)
        self.assertEqual(pending["publisher"]["diagnostics"], "display format changed")

    async def test_lost_acknowledgement_does_not_change_or_duplicate_the_committed_body(self):
        item = outcome()
        respond = self.inbox._respond
        dropped = False

        async def lose_first(connection, writer, status, document):
            nonlocal dropped

            if (not dropped) and (status == 200) and (document == item.acceptance()):
                dropped = True

                writer.close()
                await writer.wait_closed()

                return

            await respond(connection, writer, status, document)

        self.inbox._respond = lose_first

        with self.assertRaises(DeliveryUnavailable):
            await self.client.submit(item)

        await self.client.retire()
        self.assertEqual(await self.client.submit(item), item.acceptance())
        self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)
        self.assertEqual(len(self.queue.inventory(item.api_origin, item.provider_ref)), 1)

    async def _wrong_binding_refusal(self):
        endpoint = DeliveryEndpoint("https://private-canary.example", "provider:private-canary", self.endpoint.socket_path)
        client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)

        self.addAsyncCleanup(client.retire)

        with self.assertRaises(DeliveryRejected):
            await client.pending()

        return self.failures.call_args.args

    async def test_wrong_binding_explains_refusal_to_operator(self):
        (cause, facts) = await self._wrong_binding_refusal()

        self.assertIs(type(cause), DeliveryRequestRejected)
        self.assertIn("another API/provider", str(cause))
        self.assertEqual(owned_details(cause)["message"], str(cause))
        self.assertEqual(facts["provider_ref"], self.endpoint.provider_ref)
        self.assertEqual(facts["event"], "delivery_request_rejected")
        self.assertEqual(self.queue.inventory(self.endpoint.api_origin, self.endpoint.provider_ref), [])

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_wrong_binding_refusal_omits_request(self):
        (cause, _facts) = await self._wrong_binding_refusal()

        self.assertNotIn("private-canary", str(cause))

    async def test_lost_status_response_does_not_claim_durable_enqueue(self):
        delivery_outcome = outcome()

        await self.client.submit(delivery_outcome)

        async def lose_status(connection, writer, status, document):
            raise ConnectionResetError()

        self.inbox._respond = lose_status

        with self.assertRaises(DeliveryUnavailable) as lost:
            await self.client.status(delivery_outcome.execution_attempt_ref)

        self.assertIn("read delivery status", str(lost.exception))
        self.assertNotIn("retain", str(lost.exception).lower())
        (_, facts) = self.failures.call_args.args

        self.assertFalse(facts["durable_enqueue_confirmed"])
        self.assertEqual(self.queue.next_pending(delivery_outcome.api_origin, delivery_outcome.provider_ref), delivery_outcome)

    async def test_shutdown_joins_committing_write_after_waiter_cancellation(self):
        (started, release_storage) = (threading.Event(), threading.Event())
        item = outcome()

        def blocked_enqueue():
            started.set()

            if not release_storage.wait(timeout=(release_timeout_seconds := 2)):
                raise TimeoutError(f"{release_storage.is_set()=} after waiting {release_timeout_seconds=}")

            return self.queue.enqueue(item)

        waiter = asyncio.create_task(self.inbox.storage(blocked_enqueue))

        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        waiter.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await waiter

        closing = asyncio.create_task(self.inbox.close())

        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        closing.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await closing

        closing = asyncio.create_task(self.inbox.close())

        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release_storage.set()
        await closing
        self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)

    async def test_stalled_connection_close_is_aborted_at_the_owned_deadline(self):
        self.inbox.request_seconds = 0.01
        request = h11.Request(method="GET", target="/pending", headers=[("Host", "delivery.invalid")])
        self.inbox._read = AsyncMock(return_value=(request, b""))
        self.inbox._dispatch = AsyncMock(return_value={})
        self.inbox._respond = AsyncMock()

        async def stalled_close():
            await asyncio.Event().wait()

        writer = Mock()
        writer.wait_closed = stalled_close

        await asyncio.wait_for(self.inbox._client(self.endpoint, None, writer), 1)
        writer.transport.abort.assert_called_once()
        (cause, facts) = self.failures.call_args.args

        self.assertIsInstance(cause, TimeoutError)
        self.assertEqual(facts["event"], "delivery_ipc_close_failed")
