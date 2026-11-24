"""One aggregate publisher replaces stale availability without replaying it."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from provider_core.api_errors import ApiFailure
from provider_core.hello_publisher import HelloPublisher
from provider_core.transport import ExchangeFailed
from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering
from tests.fakes.api import Transport, api, reply, response_headers
from tests.fakes.hello import receipt


class HelloPublisherTests(unittest.IsolatedAsyncioTestCase):
    def publisher(self, transport, offerings, stopping, **policy):
        return HelloPublisher(
            api(transport, "https://api.example"),
            provider_ref="provider:example",
            display_name="Test provider",
            description="Independent scientific offerings",
            offerings=offerings,
            stopping=stopping,
            **policy,
        )

    def kinds(self, transport):
        return [
            [offering["analysis_kind_ref"] for offering in json.loads(request.content)["analysis_offerings"]]
            for (request, _maximum_bytes) in transport.calls
        ]

    async def test_worker_changes_during_send_replace_the_whole_snapshot_before_heartbeat(self):
        sent = asyncio.Event()
        acknowledge = asyncio.Event()
        stopping = asyncio.Event()
        offerings = tuple(AnalysisOffering(kind, kind) for kind in ("first", "second", "independent"))

        class DelayedTransport(Transport):
            async def exchange(self, request, **options):
                response = await super().exchange(request, **options)

                if len(self.calls) == 1:
                    sent.set()
                    await acknowledge.wait()
                else:
                    stopping.set()

                return response

        transport = DelayedTransport(reply(receipt()), reply(receipt()))
        publisher = self.publisher(transport, lambda: offerings, stopping)
        original = asyncio.create_task(publisher.run(refresh_seconds=3600, poll_seconds=3600))

        try:
            await asyncio.wait_for(sent.wait(), 1)

            # Both offerings on a lost host disappear from the replacement;
            # the independently available worker remains in the same snapshot.
            offerings = offerings[2:]

            acknowledge.set()
            await asyncio.wait_for(original, 1)
            self.assertEqual(self.kinds(transport), [["first", "second", "independent"], ["independent"]])
        finally:
            stopping.set()
            acknowledge.set()
            await original
            await publisher.retire()

    async def test_retry_samples_current_inventory_after_retiring_the_failed_exchange(self):
        stopping = asyncio.Event()
        offerings = (AnalysisOffering("first", "First worker"),)
        transport = Transport(reply(b"upstream unavailable", status=503), reply(receipt()))

        def retry(failure):
            nonlocal offerings
            self.assertTrue(publisher.api.can_refresh_hello(failure))
            self.assertTrue(publisher.retirement_confirmed)
            offerings = ()

            return 0.001

        publisher = self.publisher(transport, lambda: offerings, stopping, retry_policy=retry)

        try:
            await publisher.refresh()
            self.assertEqual(self.kinds(transport), [["first"], []])
            self.assertFalse(publisher.withdrawal_required)
        finally:
            await publisher.retire()

    async def test_stop_during_recovery_does_not_send_another_availability_snapshot(self):
        stopping = asyncio.Event()
        transport = Transport(reply(b"upstream unavailable", status=503), reply(receipt()))

        def retry(failure):
            self.assertTrue(publisher.api.can_refresh_hello(failure))
            stopping.set()

            return 10

        publisher = self.publisher(
            transport,
            lambda: (AnalysisOffering("first", "First worker"),),
            stopping,
            retry_policy=retry,
        )

        try:
            await asyncio.wait_for(publisher.refresh(), 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(publisher.withdrawal_required)
            await publisher.withdraw()
            self.assertEqual(self.kinds(transport), [["first"], []])
        finally:
            await publisher.retire()

    async def test_retry_wait_preserves_nominal_cadence_and_server_floor(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                offerings = (AnalysisOffering("first", "First worker"),)

                transport = Transport(
                    reply(
                        b"upstream unavailable",
                        status=503,
                        headers=(*response_headers(status=503), (b"retry-after", b"120")),
                    ),
                    reply(receipt()),
                )

                publisher = self.publisher(
                    transport,
                    lambda: offerings,
                    asyncio.Event(),
                    retry_policy=lambda failure: 60,
                )

                async def wait(seconds, *, minimum_delay_seconds):
                    nonlocal offerings
                    self.assertTrue(publisher.retirement_confirmed)
                    self.assertEqual((seconds, minimum_delay_seconds), (60, 120))
                    offerings = ()

                    return interrupted

                try:
                    accepted = await publisher.refresh(retry_wait=wait)
                    expected = [["first"]] if interrupted else [["first"], []]

                    self.assertEqual(self.kinds(transport), expected)
                    self.assertEqual(accepted is None, interrupted)
                finally:
                    await publisher.retire()

    async def test_failed_retirement_leaves_original_owner_for_enclosing_cleanup(self):
        transport = Transport(reply(b"upstream unavailable", status=503), reply(receipt()))
        owner = object()
        pending = OSError("Fixture exchange cleanup remains unconfirmed")
        transport.retire = AsyncMock(side_effect=[pending, None])

        publisher = self.publisher(
            transport,
            lambda: (AnalysisOffering("first", "First worker"),),
            asyncio.Event(),
            retry_policy=lambda failure: self.fail("Unretired exchange must not authorize retry"),
            owner=owner,
        )

        publish = publisher.api.publish_hello
        publisher.api.publish_hello = AsyncMock(wraps=publish)

        with self.assertRaises(OSError) as caught:
            await publisher.refresh()

        self.assertIs(caught.exception, pending)
        self.assertEqual(len(transport.calls), 1)
        self.assertIs(publisher.api.publish_hello.call_args.kwargs["owner"], owner)
        transport.retire.assert_awaited_once_with(owner=owner)
        await publisher.api.retire(owner=owner)
        self.assertEqual(transport.retire.await_count, 2)
        self.assertTrue(publisher.withdrawal_required)

    async def test_policy_refusal_preserves_the_failure_without_another_send(self):
        transport = Transport(ExchangeFailed("Unclassified transport failure"))
        rejected = []

        def retry_policy(failure):
            self.assertFalse(publisher.api.can_refresh_hello(failure))
            rejected.append(failure)
            raise failure

        publisher = self.publisher(
            transport,
            lambda: (AnalysisOffering("first", "First worker"),),
            asyncio.Event(),
            retry_policy=retry_policy,
        )

        try:
            with self.assertRaises(ApiFailure) as caught:
                await publisher.refresh()

            self.assertEqual(rejected, [caught.exception])
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(publisher.withdrawal_required)
        finally:
            await publisher.retire()

    async def test_lost_withdrawal_reply_retains_the_duty_until_confirmed(self):
        transport = Transport(reply(receipt()), ExchangeFailed("Withdrawal acknowledgement lost"), reply(receipt()))

        publisher = self.publisher(
            transport,
            lambda: (AnalysisOffering("first", "First worker"),),
            asyncio.Event(),
        )

        try:
            await publisher.refresh()

            with self.assertRaises(ApiFailure):
                await publisher.withdraw()

            self.assertTrue(publisher.withdrawal_required)
            await publisher.withdraw()
            self.assertFalse(publisher.withdrawal_required)
            self.assertEqual(self.kinds(transport), [["first"], [], []])
        finally:
            await publisher.retire()
