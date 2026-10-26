"""Actual TLS failures retain received protocol metadata, never partial success."""

import unittest

import httpx

from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.tls_peer import TlsPeer


class ReceivedHeadTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_or_stalled_body_preserves_the_received_head(self):
        for mode in ("disconnect", "body-stall"):
            with self.subTest(mode=mode):
                async with TlsPeer(mode) as peer:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=0.15, retirement_seconds=0.3)
                    try:
                        with self.assertRaises(ExchangeFailed) as caught:
                            await transport.exchange(httpx.Request("GET", peer.origin + "/input"), maximum_bytes=100)
                        head = caught.exception.response_head
                        self.assertEqual((head.status, head.headers), (200, ((b"Content-Length", b"100"),)))
                        self.assertIsNotNone(caught.exception.primary)
                        self.assertEqual(len(peer.requests), 1)
                    finally:
                        await transport.retire()
