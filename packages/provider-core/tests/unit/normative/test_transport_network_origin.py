"""Identical exceptions from network and local consumers keep distinct origins."""

import ssl
import unittest
from unittest.mock import patch

import httpcore
import httpx

from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.transport import Client, Stream


class TransportNetworkOriginTests(unittest.IsolatedAsyncioTestCase):
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
