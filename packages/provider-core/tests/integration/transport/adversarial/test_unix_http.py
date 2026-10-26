"""Unknown IPC acknowledgement stays distinct from API receipt and new execution."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from provider_core.network import _OwnedStream
from provider_core.transport import ExchangeFailed
from provider_core.unix_http import DELIVERY_ORIGIN, UnixHttpTransport
from tests.fakes.unix_http import UnixHTTPPeer


class UnixHTTPRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_cannot_select_a_tcp_destination_or_another_origin(self):
        async with UnixHTTPPeer() as peer:
            transport = UnixHttpTransport(socket_path=peer.path, exchange_seconds=1, retirement_seconds=0.3)
            with patch("httpcore.AnyIOBackend.connect_tcp", new=AsyncMock(side_effect=AssertionError("TCP fallback"))):
                for origin in ("https://delivery.invalid", "http://other.invalid", "http://delivery.invalid:81"):
                    with self.subTest(origin=origin), self.assertRaises(ValueError):
                        await transport.exchange(httpx.Request("GET", origin), maximum_bytes=2)
            self.assertEqual(peer.requests, [])

    async def test_response_limit_retains_origin_head_without_connect_evidence(self):
        async with UnixHTTPPeer() as peer:
            transport = UnixHttpTransport(socket_path=peer.path, exchange_seconds=1, retirement_seconds=0.3)
            with self.assertRaises(ExchangeFailed) as caught:
                await transport.exchange(httpx.Request("GET", DELIVERY_ORIGIN), maximum_bytes=1)
            self.assertEqual(caught.exception.response_head.status, 200)
            self.assertIsNone(caught.exception.proxy_connect_status)
            self.assertTrue(transport.retirement_confirmed)

    async def test_cancelled_submit_retains_original_cleanup_until_it_settles(self):
        async with UnixHTTPPeer(stall=True) as peer:
            transport = UnixHttpTransport(socket_path=peer.path, exchange_seconds=1, retirement_seconds=0.05)
            owner = object()
            closing, release = asyncio.Event(), asyncio.Event()
            close = _OwnedStream.aclose

            async def held_close(stream):
                closing.set()
                await release.wait()
                await close(stream)

            with patch.object(_OwnedStream, "aclose", held_close):
                task = asyncio.create_task(transport.exchange(
                    httpx.Request("POST", DELIVERY_ORIGIN + "/submit", content=b"ONE_OUTCOME"), maximum_bytes=2, owner=owner
                ))
                try:
                    await asyncio.wait_for(peer.head_sent.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await asyncio.wait_for(closing.wait(), 1)
                    self.assertFalse(transport.retirement_confirmed)
                    original = transport._active
                    with self.assertRaises(ExchangeFailed):
                        await transport.exchange(httpx.Request("POST", DELIVERY_ORIGIN), maximum_bytes=2, owner=owner)
                    self.assertIs(transport._active, original)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    await transport.retire(owner=owner)
            self.assertEqual(len(peer.requests), 1)
            self.assertEqual(peer.requests[0][1], b"ONE_OUTCOME")
            self.assertIsNone(transport.last_failure.proxy_connect_status)
