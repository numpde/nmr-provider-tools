"""Unix-route failures never fall back or resend; original cleanup remains owned."""

import asyncio
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpcore
import httpx

from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.unix_proxy import UnixProxy


class UnixTunnelFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_connect_retains_only_its_parsed_status_not_an_origin_head(self):
        async with TlsPeer("success") as peer:
            for status in (403, 502, 503, 504):
                with self.subTest(status=status):
                    async with UnixProxy(peer.origin, "refuse", refusal_status=status) as proxy:
                        transport = HttpTransport(
                            trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3, proxy_socket=proxy.path
                        )
                        with self.assertRaises(ExchangeFailed) as caught:
                            await transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
                        await transport.retire()
                        self.assertEqual(caught.exception.proxy_connect_status, status)
                        self.assertIsNone(caught.exception.response_head)
                        self.assertIs(type(caught.exception.primary), httpcore.ProxyError)
                        self.assertEqual(len(proxy.headers), 1)
                        self.assertEqual(peer.requests, [])

    async def test_missing_refused_or_malformed_proxy_never_attempts_direct_tcp(self):
        async with TlsPeer("success") as peer:
            for mode in ("refuse", "malformed"):
                with self.subTest(mode=mode):
                    async with UnixProxy(peer.origin, mode) as proxy:
                        transport = HttpTransport(
                            trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3, proxy_socket=proxy.path
                        )
                        with (
                            patch(
                                "httpcore.AnyIOBackend.connect_tcp",
                                new=AsyncMock(side_effect=AssertionError("direct TCP attempted")),
                            ) as tcp,
                            self.assertRaises(ExchangeFailed),
                        ):
                            await transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
                        tcp.assert_not_awaited()
                        await transport.retire()
                        self.assertEqual(len(proxy.headers), 1)
                        self.assertEqual(peer.requests, [])
            with tempfile.TemporaryDirectory() as directory:
                transport = HttpTransport(
                    trust=peer.trust,
                    exchange_seconds=1,
                    retirement_seconds=0.3,
                    proxy_socket=str(Path(directory) / "missing.sock"),
                )
                with (
                    patch(
                        "httpcore.AnyIOBackend.connect_tcp", new=AsyncMock(side_effect=AssertionError("direct TCP attempted"))
                    ) as tcp,
                    self.assertRaises(ExchangeFailed),
                ):
                    await transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
                tcp.assert_not_awaited()
                await transport.retire()
            self.assertEqual(peer.requests, [])

    async def test_lost_post_reply_does_not_resend_and_connect_receives_no_credentials(self):
        async with TlsPeer("success", response_body=None) as peer, UnixProxy(peer.origin, "tunnel") as proxy:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3, proxy_socket=proxy.path)
            request = httpx.Request(
                "POST", peer.origin + "/chat", headers={"Authorization": "Bearer PRIVATE_MODEL_KEY"}, content=b"PRIVATE_BODY"
            )
            with self.assertRaises(ExchangeFailed):
                await transport.exchange(request, maximum_bytes=4)
            await transport.retire()
            self.assertEqual(len(peer.requests), 1)
            self.assertEqual(peer.request_bodies, [b"PRIVATE_BODY"])
            self.assertIn(b"Bearer PRIVATE_MODEL_KEY", peer.requests[0])
            self.assertEqual(len(proxy.headers), 1)
            self.assertNotIn(b"PRIVATE", proxy.headers[0])
            self.assertNotIn(b"authorization", proxy.headers[0].lower())

    async def test_connect_and_tls_stalls_share_aggregate_deadline_and_cancel_closes_tunnel(self):
        for peer_mode, proxy_mode in (("success", "stall"), ("tls-stall", "tunnel")):
            for interrupt in (False, True):
                with self.subTest(peer=peer_mode, cancel=interrupt):
                    async with TlsPeer(peer_mode) as peer, UnixProxy(peer.origin, proxy_mode) as proxy:
                        transport = HttpTransport(
                            trust=peer.trust, exchange_seconds=0.15, retirement_seconds=0.3, proxy_socket=proxy.path
                        )
                        started = asyncio.get_running_loop().time()
                        task = asyncio.create_task(transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4))
                        if interrupt:
                            await asyncio.wait_for(proxy.received.wait(), 1)
                            if peer_mode == "tls-stall":
                                await asyncio.wait_for(peer.received.wait(), 1)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        else:
                            with self.assertRaises(ExchangeFailed) as caught:
                                await task
                            self.assertIsInstance(caught.exception.primary, TimeoutError)
                        await transport.retire()
                        self.assertLess(asyncio.get_running_loop().time() - started, 0.8)
                        await asyncio.wait_for(proxy.closed.wait(), 1)
                        self.assertTrue(transport.retirement_confirmed)
                        self.assertEqual(peer.requests, [])

    async def test_wrong_destination_certificate_is_rejected_before_application_bytes(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        peer_failures = []

        def observe_peer_failure(_loop, context):
            peer_failures.append(context)

        # This deliberate client-side certificate rejection can reset the test
        # server's handshake before its callback runs. Own those loop reports;
        # unexpected exception types still fail the test below.
        loop.set_exception_handler(observe_peer_failure)
        try:
            async with TlsPeer("success") as peer, TlsPeer("success") as unrelated, UnixProxy(peer.origin, "tunnel") as proxy:
                transport = HttpTransport(
                    trust=unrelated.trust, exchange_seconds=1, retirement_seconds=0.3, proxy_socket=proxy.path
                )
                with self.assertRaises(ExchangeFailed) as caught:
                    await transport.exchange(httpx.Request("POST", peer.origin, content=b"PRIVATE_BODY"), maximum_bytes=4)
                await transport.retire()
                self.assertIsInstance(caught.exception.primary, httpcore.ConnectError)
                self.assertEqual(caught.exception.proxy_connect_status, 200)
                causes, pending = [], [caught.exception.primary]
                while pending:
                    cause = pending.pop()
                    if cause is not None and all(cause is not seen for seen in causes):
                        causes.append(cause)
                        pending.extend((cause.__cause__, cause.__context__))
                self.assertTrue(any(isinstance(cause, ssl.SSLCertVerificationError) for cause in causes))
                self.assertEqual(peer.requests, [])
                self.assertEqual(len(proxy.headers), 1)
        finally:
            loop.set_exception_handler(previous)
        for context in peer_failures:
            self.assertIsInstance(context.get("exception"), (ConnectionResetError, ssl.SSLError))
