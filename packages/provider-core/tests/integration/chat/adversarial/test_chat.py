"""Chat failures preserve private causes without retries or leaked model text."""

import asyncio
import json
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from provider_core.chat import ChatEndpoint, ChatEndpointSpec, ChatFailed
from provider_core.transport import ExchangeFailed, HttpResponse, HttpTransport
from tests.fakes.tls_peer import TlsPeer


class ChatCarrierRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_rejection_keeps_error_fields_without_complete_prompt_or_key_echoes(self):
        prompt = 'PRIVATE_INPUT quoted "source"\nC 0 0 0; api_key=PRIVATE_KEY'
        body = json.dumps({"error": {
            "message": "Unsupported tool_choice. Echo: " + prompt + "; escaped: " + json.dumps(prompt)[1:-1],
            "type": "invalid_request_error", "code": "unsupported_parameter", "param": "tool_choice",
            "request": "PRIVATE_UNRELATED_BODY",
        }}).encode()
        response = HttpResponse(400, ((b"content-type", b"application/json"),), body)
        transport = SimpleNamespace(exchange=AsyncMock(return_value=response), retire=AsyncMock(), retirement_confirmed=True)
        endpoint = ChatEndpoint(self.spec("https://example.invalid/chat"), transport=transport,
                                request_bytes=4096, response_bytes=16384)
        with self.assertRaises(ChatFailed) as caught:
            await endpoint.complete([{"role": "user", "content": prompt}], [])
        evidence = caught.exception.endpoint_error
        self.assertIn("Unsupported tool_choice", evidence["message"])
        self.assertEqual(evidence["code"], "unsupported_parameter")
        self.assertEqual(evidence["param"], "tool_choice")
        self.assertNotIn("PRIVATE", json.dumps(evidence))
        self.assertNotIn("C 0 0 0", json.dumps(evidence))
        self.assertIs(caught.exception.response, response)
        transport.exchange.assert_awaited_once()

    async def test_unreadable_error_detail_preserves_status_without_partial_body(self):
        for body in (b"<html>PRIVATE</html>", b'{"error":{"message":"PRIVATE',
                     json.dumps({"error": {"message": "PRIVATE" * 2000}}).encode()):
            with self.subTest(length=len(body)):
                response = HttpResponse(400, (), body)
                transport = SimpleNamespace(exchange=AsyncMock(return_value=response), retire=AsyncMock(), retirement_confirmed=True)
                endpoint = ChatEndpoint(self.spec("https://example.invalid/chat"), transport=transport,
                                        request_bytes=4096, response_bytes=32768)
                with self.assertRaises(ChatFailed) as caught:
                    await endpoint.complete([], [])
                self.assertEqual(caught.exception.response.status, 400)
                self.assertIn("detail_unavailable", caught.exception.endpoint_error)
                self.assertNotIn("PRIVATE", json.dumps(caught.exception.endpoint_error))

    async def test_interruption_keeps_exact_transport_primary_and_its_previous_explicit_cause(self):
        previous = OSError("PRIVATE earlier interruption cause")
        interruption = asyncio.CancelledError()
        interruption.__cause__ = previous
        failure = ExchangeFailed("Authored interrupted exchange", primary=interruption)
        transport = SimpleNamespace(exchange=AsyncMock(side_effect=interruption), last_failure=failure)
        endpoint = ChatEndpoint(
            self.spec("https://example.invalid/chat"), transport=transport, request_bytes=4096, response_bytes=4096
        )
        with self.assertRaises(asyncio.CancelledError) as caught:
            await endpoint.complete([], [])
        self.assertIs(caught.exception, interruption)
        self.assertIs(type(interruption.__cause__), ChatFailed)
        self.assertIs(interruption.__cause__.cause, failure)
        self.assertIs(failure.primary, interruption)
        self.assertIs(interruption.__cause__.interruption_cause, previous)
        transport.exchange.assert_awaited_once()

    async def test_peer_retires_an_observed_tls_handshake_that_never_reaches_its_http_handler(self):
        writer = None
        try:
            async with TlsPeer("success") as peer:
                incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
                client = peer.trust.wrap_bio(incoming, outgoing, server_side=False, server_hostname="localhost")
                with self.assertRaises(ssl.SSLWantReadError):
                    client.do_handshake()
                port = peer.server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(outgoing.read())
                await writer.drain()
                await asyncio.wait_for(peer.handshake_received.wait(), 1)
                self.assertEqual(peer.server_names, ["localhost"])
                self.assertEqual(peer.handlers, set())
            await asyncio.wait_for(reader.read(), 1)
            self.assertEqual(peer.requests, [])
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()

    def spec(self, url):
        return ChatEndpointSpec(
            configuration_id="local-test", model="fixture-model", url=url, api_key="PRIVATE_KEY", reasoning_effort=None
        )

    async def test_oversized_request_is_rejected_before_http(self):
        transport = SimpleNamespace(exchange=AsyncMock(), retire=AsyncMock(), retirement_confirmed=True)
        endpoint = ChatEndpoint(
            self.spec("https://example.invalid/chat"), transport=transport, request_bytes=64, response_bytes=4096
        )
        with self.assertRaisesRegex(ChatFailed, "request.*byte limit"):
            await endpoint.complete([{"role": "user", "content": "PRIVATE_SOURCE" * 100}], [])
        transport.exchange.assert_not_awaited()

    async def test_http_failure_and_malformed_replies_keep_raw_evidence_private(self):
        for status, body in (
            (401, b"PRIVATE_KEY PRIVATE_SOURCE"),
            (200, b'{"choices":[],"choices":[]}'),
            (200, b'{"choices": []}'),
            (200, b"PRIVATE_SOURCE invalid json"),
            (200, json.dumps({"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]}).encode()),
            (200, json.dumps({"choices": [{"message": {"role": "assistant"}, "finish_reason": None}]}).encode()),
            (200, json.dumps({"choices": [{"message": {"role": "assistant"}, "finish_reason": "content_filter"}]}).encode()),
        ):
            with self.subTest(status=status, body=body):
                response = HttpResponse(status, ((b"content-type", b"application/json"),), body)
                transport = SimpleNamespace(
                    exchange=AsyncMock(return_value=response), retire=AsyncMock(), retirement_confirmed=True
                )
                endpoint = ChatEndpoint(
                    self.spec("https://example.invalid/chat"), transport=transport, request_bytes=4096, response_bytes=4096
                )
                with self.assertRaises(ChatFailed) as caught:
                    await endpoint.complete([], [])
                transport.exchange.assert_awaited_once()
                self.assertIs(caught.exception.response, response)
                self.assertNotIn("PRIVATE", str(caught.exception))

    async def test_consumed_response_limit_and_trickle_deadline_retire_actual_connections(self):
        for mode, maximum in (("success", 16), ("trickle", 4096)):
            with self.subTest(mode=mode):
                async with TlsPeer(mode, response_body=b"x" * 100) as peer:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=0.15, retirement_seconds=0.5)
                    endpoint = ChatEndpoint(
                        self.spec(peer.origin + "/chat"), transport=transport, request_bytes=4096, response_bytes=maximum
                    )
                    self.addAsyncCleanup(endpoint.retire)
                    with self.assertRaises(ChatFailed) as caught:
                        await asyncio.wait_for(endpoint.complete([], []), 2)
                    self.assertIsInstance(caught.exception.cause, ExchangeFailed)
                    self.assertTrue(endpoint.retirement_confirmed)
                    self.assertEqual(len(peer.requests), 1)
                    await asyncio.wait_for(peer.closed.wait(), 1)

    async def test_cancelled_exchange_is_retired_and_remains_cancellation(self):
        async with TlsPeer("headers-stall") as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            endpoint = ChatEndpoint(
                self.spec(peer.origin + "/chat"), transport=transport, request_bytes=4096, response_bytes=4096
            )
            self.addAsyncCleanup(endpoint.retire)
            task = asyncio.create_task(endpoint.complete([], []))
            await asyncio.wait_for(peer.received.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(endpoint.retirement_confirmed)
            await asyncio.wait_for(peer.closed.wait(), 1)

    async def test_unconfirmed_retirement_preserves_the_transport_failure(self):
        failure = ExchangeFailed("Resource retirement is unconfirmed", primary=OSError("PRIVATE"))
        transport = SimpleNamespace(
            exchange=AsyncMock(side_effect=failure), retire=AsyncMock(side_effect=failure), retirement_confirmed=False
        )
        endpoint = ChatEndpoint(
            self.spec("https://example.invalid/chat"), transport=transport, request_bytes=4096, response_bytes=4096
        )
        with self.assertRaises(ChatFailed) as caught:
            await endpoint.complete([], [])
        self.assertIs(caught.exception.cause, failure)
        self.assertFalse(endpoint.retirement_confirmed)
        with self.assertRaises(ChatFailed) as retired:
            await endpoint.retire()
        self.assertIs(retired.exception.cause, failure)

    async def test_lost_reply_does_not_repeat_the_potentially_billed_post(self):
        async def lose_reply(_request, _body):
            return None

        async with TlsPeer("success", response_factory=lose_reply) as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            endpoint = ChatEndpoint(
                self.spec(peer.origin + "/chat"), transport=transport, request_bytes=4096, response_bytes=4096
            )
            self.addAsyncCleanup(endpoint.retire)
            with self.assertRaises(ChatFailed):
                await endpoint.complete([], [])
            self.assertEqual(len(peer.requests), 1)
            self.assertTrue(endpoint.retirement_confirmed)

    def test_non_https_credentials_queries_and_fragments_are_not_endpoint_authority(self):
        for url in (
            "http://example.invalid/chat",
            "https://user:secret@example.invalid/chat",
            "https://example.invalid/chat?secret=x",
            "https://example.invalid/chat#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.spec(url)
