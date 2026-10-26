"""Delivery IPC uses plain HTTP on one Unix grant, with no TCP or TLS fallback."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from provider_core.unix_http import DELIVERY_ORIGIN, UnixHttpTransport
from tests.fakes.unix_http import UnixHTTPPeer


class UnixHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_body_uses_only_the_admitted_unix_socket(self):
        async with UnixHTTPPeer() as peer:
            transport = UnixHttpTransport(socket_path=peer.path, exchange_seconds=1, retirement_seconds=0.3)
            with patch("httpcore.AnyIOBackend.connect_tcp", new=AsyncMock(side_effect=AssertionError("TCP fallback"))) as tcp:
                reply = await transport.exchange(
                    httpx.Request("POST", DELIVERY_ORIGIN + "/submit", content=b"EXACT_BODY\n"), maximum_bytes=2
                )
            self.assertEqual((reply.status, reply.body), (200, b"ok"))
            self.assertEqual(peer.requests[0][1], b"EXACT_BODY\n")
            self.assertTrue(peer.requests[0][0].startswith(b"POST /submit HTTP/1.1\r\n"))
            tcp.assert_not_awaited()
            self.assertTrue(transport.retirement_confirmed)
