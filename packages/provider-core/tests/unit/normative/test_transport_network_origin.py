"""Identical exceptions from network and local consumers keep distinct origins."""

import errno
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpcore
import httpx

from provider_core.api import ApiFailure
from provider_core.transport import ExchangeFailed, HttpTransport, temporary_io_failure
from tests.fakes.api import api
from tests.fakes.hello import announcement
from tests.fakes.transport import Client, Stream


class TransportNetworkOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_unix_proxy_refusal_permits_fresh_hello_after_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                path.chmod(0o600)
            transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1,
                                      retirement_seconds=1, proxy_socket=str(path))
            client = api(transport, "https://api.example")
            with self.assertRaises(ApiFailure) as caught:
                await client.publish_hello(**announcement())
            failure = caught.exception
            self.assertTrue(transport.retirement_confirmed)
            self.assertIs(type(failure.cause.primary), httpcore.ConnectError)
            self.assertTrue(client.can_refresh_hello(failure))

    def test_context_is_used_only_for_exact_connect_wrapper_without_explicit_cause(self):
        refused = ConnectionRefusedError(errno.ECONNREFUSED, "private network detail")
        connect = httpcore.ConnectError("private wrapper")
        connect.__context__ = refused
        self.assertTrue(temporary_io_failure(connect, 8))
        for explicit in (PermissionError(errno.EACCES, "private permission"), ssl.SSLCertVerificationError(1, "private TLS")):
            connect.__cause__ = explicit
            self.assertFalse(temporary_io_failure(connect, 8))
        for incidental in (OSError("private local failure"), RuntimeError("private local failure"),
                           httpcore.ReadError("private read"), httpcore.WriteError("private write")):
            incidental.__context__ = refused
            self.assertFalse(temporary_io_failure(incidental, 8))
        cycle = httpcore.ConnectError("private cyclic context")
        cycle.__context__ = cycle
        self.assertFalse(temporary_io_failure(cycle, 8))

    async def test_same_native_exception_from_stream_and_account_has_distinct_origin(self):
        original = httpcore.ReadError("private detail")
        for origin, chunks, stream_failure in (("network", [], original), ("account", [b"x"], None)):
            with self.subTest(origin=origin):
                client = Client(Stream(chunks, failure=stream_failure))
                transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=1)
                def account(_count):
                    raise original
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
                    with self.assertRaises(ExchangeFailed) as caught:
                        await transport.exchange(httpx.Request("GET", "https://store.example/bytes"),
                                                 maximum_bytes=1, account_bytes=account)
                self.assertIs(caught.exception.primary, original)
                self.assertIs(caught.exception.network_failure, stream_failure)
                self.assertTrue(transport.retirement_confirmed)

    async def test_request_connection_failure_retains_exact_network_origin(self):
        original = httpcore.ConnectTimeout("private detail")
        client = Client(Stream([]))
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=1)
        with patch.object(client, "handle_async_request", side_effect=original), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
        ):
            with self.assertRaises(ExchangeFailed) as caught:
                await transport.exchange(httpx.Request("GET", "https://store.example/bytes"), maximum_bytes=1)
        self.assertIs(caught.exception.primary, original)
        self.assertIs(caught.exception.network_failure, original)
        self.assertTrue(transport.retirement_confirmed)
