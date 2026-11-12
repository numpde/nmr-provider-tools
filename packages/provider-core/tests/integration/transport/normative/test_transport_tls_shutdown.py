"""A completed response must leave both TLS peers able to close cleanly."""

import asyncio
from contextlib import AsyncExitStack
import socket
import unittest

import httpx
import httpcore

from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.unix_proxy import UnixProxy


class TlsShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_headers_keep_the_original_failure_while_unread_body_is_aborted(self):
        """A TLS goodbye must not replace the error that stopped the request."""
        async with TlsPeer(
            "success", response_headers=((b"bad header", b"value"),),
            response_body=b"unread" * 16384,
        ) as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
            with self.assertRaises(ExchangeFailed) as caught:
                await transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
            self.assertIsInstance(caught.exception.primary, httpcore.RemoteProtocolError)
            self.assertEqual(caught.exception.cleanup_failures, ())
            self.assertTrue(transport.retirement_confirmed)

    async def test_complete_response_closes_tls_before_direct_socket(self):
        await self._assert_clean_shutdown(through_proxy=False)

    async def test_complete_response_closes_tls_before_unix_tunnel(self):
        await self._assert_clean_shutdown(through_proxy=True)

    async def _assert_clean_shutdown(self, *, through_proxy):
        """The server must receive the TLS goodbye, rather than a dropped socket."""
        async with AsyncExitStack() as stack:
            peer = await stack.enter_async_context(TlsPeer("success"))
            listener = stack.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)
            origin = f"https://127.0.0.1:{listener.getsockname()[1]}"
            proxy_path = None
            if through_proxy:
                proxy = await stack.enter_async_context(UnixProxy(origin, "tunnel"))
                proxy_path = proxy.path

            def serve():
                # A strict TLS socket distinguishes a proper goodbye from raw
                # EOF. asyncio's ordinary reader hides that distinction.
                raw, _address = listener.accept()
                with raw:
                    raw.settimeout(3)
                    with peer.server_trust.wrap_socket(
                        raw, server_side=True, suppress_ragged_eofs=False,
                    ) as connection:
                        request = bytearray()
                        while not request.endswith(b"\r\n\r\n"):
                            chunk = connection.recv(1)
                            if not chunk or len(request) >= 16384:
                                raise ValueError("Test server did not receive a bounded HTTP request")
                            request.extend(chunk)
                        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndata")
                        # Read after the complete response: a missing client
                        # goodbye raises SSLEOFError even though HTTP succeeded.
                        closed = connection.recv(1)
                        connection.unwrap().close()
                        return closed

            transport = HttpTransport(
                trust=peer.trust, exchange_seconds=2, retirement_seconds=0.5,
                proxy_socket=proxy_path,
            )
            response, closed = await asyncio.gather(
                transport.exchange(httpx.Request("GET", origin + "/input"), maximum_bytes=4),
                asyncio.to_thread(serve),
                return_exceptions=True,
            )
            self.assertNotIsInstance(response, BaseException)
            self.assertEqual(response.body, b"data")
            self.assertEqual(closed, b"", "The server must observe a clean TLS shutdown")
            self.assertTrue(transport.retirement_confirmed)
