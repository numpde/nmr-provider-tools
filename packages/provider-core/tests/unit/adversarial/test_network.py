"""Raw streams remain owned when TLS upgrade or its cleanup fails."""

import asyncio
import ssl
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.network import OwnedNetworkBackend


class RawStream:
    def __init__(self):
        self.close_failure = None
        self.closed = False

    async def start_tls(self, **options):
        raise asyncio.CancelledError

    async def aclose(self):
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True


class Backend:
    def __init__(self, raw):
        self.raw = raw

    async def connect_tcp(self, **options):
        return self.raw


class NetworkOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_backend_uses_only_the_fixed_unix_path_and_retains_cancelled_tls_cleanup(self):
        raw = RawStream()
        native = Backend(raw)
        native.connect_unix_socket = AsyncMock(return_value=raw)
        native.connect_tcp = AsyncMock(side_effect=AssertionError("direct TCP attempted"))
        with patch("provider_core.network.httpcore.AnyIOBackend", return_value=native):
            backend = OwnedNetworkBackend(proxy_socket="/egress/proxy.sock")
        stream = await backend.connect_tcp("proxy-metadata.invalid", 80, timeout=0.5)
        native.connect_unix_socket.assert_awaited_once_with(path="/egress/proxy.sock", timeout=0.5, socket_options=None)
        with self.assertRaises(asyncio.CancelledError):
            await stream.start_tls(ssl.create_default_context(), "api.example")
        raw.close_failure = OSError("PRIVATE close")
        with self.assertRaises(OSError):
            await backend.aclose()
        self.assertFalse(raw.closed)
        raw.close_failure = None
        await backend.aclose()
        self.assertTrue(raw.closed)
        native.connect_tcp.assert_not_awaited()

    async def test_cancelled_tls_upgrade_keeps_raw_stream_until_close_succeeds(self):
        raw = RawStream()
        with patch("provider_core.network.httpcore.AnyIOBackend", return_value=Backend(raw)):
            backend = OwnedNetworkBackend()
        stream = await backend.connect_tcp("127.0.0.1", 443)
        with self.assertRaises(asyncio.CancelledError):
            await stream.start_tls(ssl.create_default_context(), "localhost")
        failure = RuntimeError("close failed")
        raw.close_failure = failure
        with self.assertRaises(RuntimeError) as caught:
            await backend.aclose()
        self.assertIs(caught.exception, failure)
        self.assertFalse(raw.closed)
        raw.close_failure = None
        await backend.aclose()
        self.assertTrue(raw.closed)
