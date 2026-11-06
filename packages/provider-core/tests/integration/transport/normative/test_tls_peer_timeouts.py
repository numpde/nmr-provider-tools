"""The shared test TLS peer owns finite handshake and shutdown limits.

These settings apply to the synthetic incoming TLS server. They do not
change the SDK client's own connect, exchange or child-retirement budget.
"""

import asyncio
from pathlib import Path
import ssl
import unittest
from unittest.mock import patch

from tests.fakes.tls_peer import TlsPeer


class TlsPeerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_allows_a_delayed_but_successful_tls_handshake(self):
        # The baseline's hardcoded 0.5-second server timeout drops a real TCP
        # connection before the client starts TLS. No application HTTP handler
        # is invoked until that handshake has completed.
        async with TlsPeer("success") as peer:
            context = ssl.create_default_context(
                cafile=str(Path(peer.scratch.name) / "cert.pem"),
            )
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", peer.server.sockets[0].getsockname()[1],
            )
            try:
                await asyncio.sleep(0.8)
                await asyncio.wait_for(
                    writer.start_tls(context, server_hostname="localhost", ssl_handshake_timeout=2),
                    2.5,
                )
                writer.write(b"GET /delayed HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                body = await asyncio.wait_for(reader.readexactly(4), 2)
                self.assertIn(b"HTTP/1.1 200 OK", head)
                self.assertEqual(b"data", body)
            finally:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 2)
            await asyncio.wait_for(peer.closed.wait(), 2)
            self.assertEqual(1, len(peer.requests))
            self.assertEqual(b"GET /delayed HTTP/1.1", peer.requests[0].split(b"\r\n")[0])

    async def test_independent_overrides_reach_the_tls_server(self):
        # Observe the supported asyncio.start_server arguments, not private
        # asyncio SSL internals. The shutdown allowance remains independent.
        original = asyncio.start_server
        with patch("tests.fakes.tls_peer.asyncio.start_server", wraps=original) as started:
            async with TlsPeer("success", handshake_seconds=1.75, shutdown_seconds=0.8) as peer:
                self.assertEqual((1.75, 0.8), (peer.handshake_seconds, peer.shutdown_seconds))
                self.assertEqual(1, started.await_count)
                selected = started.await_args.kwargs
                self.assertEqual(1.75, selected["ssl_handshake_timeout"])
                self.assertEqual(0.8, selected["ssl_shutdown_timeout"])

    async def test_raw_tcp_tls_stall_remains_an_explicit_non_ssl_mode(self):
        original = asyncio.start_server
        with patch("tests.fakes.tls_peer.asyncio.start_server", wraps=original) as started:
            async with TlsPeer("tls-stall", handshake_seconds=1.5, shutdown_seconds=0.25):
                selected = started.await_args.kwargs
                self.assertNotIn("ssl", selected)
                self.assertNotIn("ssl_handshake_timeout", selected)
                self.assertNotIn("ssl_shutdown_timeout", selected)

    def test_invalid_limits_are_rejected_before_creating_a_server(self):
        for key in ("handshake_seconds", "shutdown_seconds"):
            for value in (None, True, False, 0, -1, float("nan"), float("inf"), "3"):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    TlsPeer("success", **{key: value})
