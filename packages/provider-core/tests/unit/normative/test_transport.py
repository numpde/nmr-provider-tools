"""A response crosses the transport boundary only after owned resources close."""

import hashlib
import os
import ssl
import tempfile
import unittest
from unittest.mock import patch

import httpx

from provider_core.transport import HttpFileResponse, HttpTransport
from tests.fakes.transport import Client, Stream


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_output_is_streamed_and_borrowed_descriptor_stays_open(self):
        chunks = [b"a" * 100003, b"bc", b"d" * 80001]
        client = Client(Stream(chunks))
        charged, writes = [], []
        write = os.write

        def partial_write(descriptor, data):
            writes.append(len(data))
            return write(descriptor, data[:997])

        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=2, retirement_seconds=1)
        with (
            tempfile.TemporaryFile() as output,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            patch("provider_core.transport.os.write", side_effect=partial_write),
        ):
            response = await transport.exchange_file(
                httpx.Request("GET", "https://api.example/input"),
                output_fd=output.fileno(),
                maximum_bytes=sum(map(len, chunks)),
                account_bytes=charged.append,
            )
            self.assertIs(type(response), HttpFileResponse)
            self.assertEqual(response.byte_length, sum(map(len, chunks)))
            self.assertEqual(response.content_hash, "sha256:" + hashlib.sha256(b"".join(chunks)).hexdigest())
            self.assertFalse(hasattr(response, "body"))
            self.assertEqual(charged, list(map(len, chunks)))
            self.assertLessEqual(max(writes), 65536)
            self.assertTrue(client.closed and client.stream.closed)
            self.assertTrue(transport.retirement_confirmed)
            output.seek(0)
            self.assertEqual(output.read(), b"".join(chunks))
            os.write(output.fileno(), b"caller still owns the descriptor")

    async def test_empty_file_response_has_complete_empty_body_facts(self):
        client = Client(Stream([]), status=204)
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=1)
        with (
            tempfile.TemporaryFile() as output,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            response = await transport.exchange_file(
                httpx.Request("GET", "https://api.example/input"), output_fd=output.fileno(), maximum_bytes=0
            )
            self.assertEqual((response.status, response.byte_length), (204, 0))
            self.assertEqual(response.content_hash, "sha256:" + hashlib.sha256(b"").hexdigest())
            self.assertEqual(output.tell(), 0)

    async def test_file_facts_describe_response_bytes_at_callers_current_offset(self):
        client = Client(Stream([b"body"]))
        transport = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=1)
        with (
            tempfile.TemporaryFile() as output,
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            output.write(b"prefix:old body:tail")
            output.flush()
            output.seek(7)
            response = await transport.exchange_file(
                httpx.Request("GET", "https://api.example/input"), output_fd=output.fileno(), maximum_bytes=4
            )
            self.assertEqual(response.byte_length, 4)
            self.assertEqual(response.content_hash, "sha256:" + hashlib.sha256(b"body").hexdigest())
            output.seek(0)
            self.assertEqual(output.read(), b"prefix:bodybody:tail")

    async def test_explicit_unix_route_selects_https_proxy_without_creating_a_direct_pool(self):
        client = Client(Stream([b"ok"]))
        trust = ssl.create_default_context()
        transport = HttpTransport(trust=trust, exchange_seconds=1, retirement_seconds=1, proxy_socket="/egress/proxy.sock")
        with (
            patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=AssertionError("direct pool created")),
            patch("provider_core.transport.httpcore.AsyncHTTPProxy", return_value=client) as factory,
        ):
            response = await transport.exchange(httpx.Request("GET", "https://api.example/input"), maximum_bytes=2)
        self.assertEqual(response.body, b"ok")
        self.assertEqual(client.calls[0][0].url.host, b"api.example")
        self.assertIs(factory.call_args.kwargs["ssl_context"], trust)
        self.assertEqual(factory.call_args.kwargs["retries"], 0)
        self.assertTrue(client.closed)

    async def test_success_closes_before_handoff_and_next_call_gets_fresh_client(self):
        clients = [Client(Stream([b"ab", b"cd"])), Client(Stream([b"ok"]))]
        trust = ssl.create_default_context()
        transport = HttpTransport(trust=trust, exchange_seconds=1, retirement_seconds=1)
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients) as factory:
            response = await transport.exchange(httpx.Request("GET", "https://api.example/input"), maximum_bytes=4)
            self.assertEqual(response.body, b"abcd")
            self.assertEqual(response.status, 200)
            self.assertTrue(clients[0].closed)
            self.assertTrue(clients[0].stream.closed)
            self.assertEqual(clients[0].calls[0][0].url.target, b"/input")
            await transport.exchange(httpx.Request("GET", "https://api.example/input"), maximum_bytes=4)
        self.assertEqual(factory.call_count, 2)
        self.assertIs(factory.call_args.kwargs["ssl_context"], trust)
        self.assertEqual(factory.call_args.kwargs["retries"], 0)
        self.assertTrue(clients[1].closed)
