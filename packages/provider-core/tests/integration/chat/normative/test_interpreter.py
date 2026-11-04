"""The shared flow retries real HTTPS and preserves a long connected reply."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from provider_core.chat import ChatEndpoint, ChatEndpointSpec
from provider_core.conversation import ToolFinished
from provider_core.interpreter import Interpreter
from provider_core.network import network_availability
from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.unix_proxy import UnixProxy


class SharedInterpreterCarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_https_retry_schema_query_and_single_preparation(self):
        count = 0
        async def response(request, body):
            nonlocal count
            count += 1
            if count == 1:
                peer.response_status = 503
                return b'{"error":{"message":"temporarily unavailable"}}'
            peer.response_status = 200
            message = (call("schema", "describe_input_constraints", {"tool": "prepare"}) if count == 2
                       else call("finish", "prepare", {"format": "text"}))
            return json.dumps({"choices": [{"message": message, "finish_reason": "tool_calls"}]}).encode()

        async with TlsPeer("success", response_factory=response) as peer:
            def build(**grant):
                return ChatEndpoint(ChatEndpointSpec(configuration_id="fixture", model="fixture", url=peer.origin + "/chat",
                                                     api_key="PRIVATE_KEY", reasoning_effort=None),
                                    transport=HttpTransport(trust=peer.trust, retirement_seconds=0.5, **grant),
                                    request_bytes=32768, response_bytes=8192)
            owner = Interpreter((build,), timeout_seconds=10, context_bytes=16384, secrets=("PRIVATE_KEY",))
            dispatch = AsyncMock(return_value=ToolFinished("prepared"))
            tools = [{"type": "function", "function": {"name": "prepare", "parameters": {
                "type": "object", "properties": {"format": {"enum": ["text"]}}}}}]
            result = await owner.run([], tools, dispatch)
            self.assertEqual(result.value, "prepared")
            self.assertEqual(len(peer.requests), 3)
            self.assertEqual(json.loads(result.observations[0])["turns_started"], 2)
            dispatch.assert_awaited_once_with("prepare", {"format": "text"})
            self.assertTrue(owner.retirement_confirmed)

    async def test_setup_cap_does_not_become_a_response_timeout_direct_or_through_proxy(self):
        async def slow_response(request, body):
            await asyncio.sleep(0.3)
            return b"data"
        async with TlsPeer("success", response_factory=slow_response) as peer:
            for use_proxy in (False, True):
                async with UnixProxy(peer.origin, "tunnel") as proxy:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=0.5,
                                              connect_seconds=0.15, proxy_socket=proxy.path if use_proxy else None)
                    response = await transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
                    self.assertEqual(response.body, b"data")
                    await transport.retire()
                    self.assertTrue(transport.retirement_confirmed)

    async def test_setup_deadline_covers_stalled_connect_and_tls_before_long_response_window(self):
        for peer_mode, proxy_mode in (("success", "stall"), ("tls-stall", "tunnel")):
            with self.subTest(peer_mode=peer_mode):
                async with TlsPeer(peer_mode) as peer, UnixProxy(peer.origin, proxy_mode) as proxy:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=3, retirement_seconds=0.5,
                                              connect_seconds=0.15, proxy_socket=proxy.path)
                    start = asyncio.get_running_loop().time()
                    with self.assertRaises(ExchangeFailed) as caught:
                        await transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
                    await transport.retire()
                    self.assertLess(asyncio.get_running_loop().time() - start, 1.5)
                    failure = caught.exception
                    self.assertTrue(network_availability(failure.primary, failure.network_io, network_failure=failure.network_failure))
                    self.assertEqual(peer.requests, [])
                    self.assertTrue(transport.retirement_confirmed)

    async def test_lost_reply_after_successful_connect_retries_same_model_without_science_replay(self):
        count = 0
        async def response(request, body):
            nonlocal count
            count += 1
            if count == 1:
                return None
            return json.dumps({"choices": [{"message": call("finish", "prepare", {}), "finish_reason": "tool_calls"}]}).encode()
        async with TlsPeer("success", response_factory=response) as peer, UnixProxy(peer.origin, "tunnel") as proxy:
            def build(**grant):
                return ChatEndpoint(ChatEndpointSpec(configuration_id="fixture", model="fixture", url=peer.origin + "/chat",
                                                     api_key="PRIVATE_KEY", reasoning_effort=None),
                                    transport=HttpTransport(trust=peer.trust, proxy_socket=proxy.path,
                                                            retirement_seconds=0.5, **grant),
                                    request_bytes=32768, response_bytes=8192)
            owner = Interpreter((build,), timeout_seconds=10, context_bytes=16384, secrets=("PRIVATE_KEY",))
            dispatch = AsyncMock(return_value=ToolFinished("prepared"))
            tools = [{"type": "function", "function": {"name": "prepare", "parameters": {"type": "object"}}}]
            result = await owner.run([], tools, dispatch)
            self.assertEqual(result.value, "prepared")
            self.assertEqual(len(peer.requests), 2)
            self.assertEqual(len(proxy.headers), 2)
            dispatch.assert_awaited_once()
            self.assertTrue(owner.retirement_confirmed)
