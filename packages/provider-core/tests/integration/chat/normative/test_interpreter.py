"""The shared flow retries real HTTPS and preserves a long connected reply."""

import asyncio
import json
import unittest
from unittest.mock import ANY, AsyncMock

import httpx

from provider_core.chat import ChatEndpointSpec, ChatEndpointFactory
from provider_core.conversation import ToolFinished, ToolReply
from provider_core.interpreter import Interpreter
from provider_core.network import network_availability
from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.unix_proxy import UnixProxy


class SharedInterpreterCarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_page_reserves_the_actual_carrier_envelope(self):
        replies = [call("inspect", "inspect", {}),
                   call("history", "read_interpretation_records", {"records": [{"ref": "record-1"}]}),
                   call("finish", "prepare", {})]

        async def response(request, body):
            return json.dumps({"choices": [{"message": replies.pop(0), "finish_reason": "tool_calls"}]}).encode()

        async def dispatch(name, arguments, *, not_after):
            return ToolReply({"text": "x" * 16000}) if name == "inspect" else ToolFinished("prepared")

        async with TlsPeer("success", response_factory=response) as peer:
            factories = tuple(ChatEndpointFactory(
                spec=ChatEndpointSpec(configuration_id=name, model="fixture-" + name,
                                      url=peer.origin + "/chat", api_key="PRIVATE_KEY", reasoning_effort="high"),
                trust=peer.trust, retirement_seconds=0.5, request_bytes=6000, response_bytes=8192,
            ) for name in ("first", "second"))
            owner = Interpreter(factories, timeout_seconds=10,
                                context_bytes=min(item.context_bytes for item in factories), secrets=("PRIVATE_KEY",))
            tools = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                     for name in ("inspect", "prepare")]
            result = await owner.run([], tools, dispatch)
            self.assertEqual(result.value, "prepared")
            self.assertEqual(result.model_visit, 2)
            self.assertTrue(owner.retirement_confirmed)
            self.assertEqual(len(peer.request_bodies), 3)
            self.assertTrue(all(len(body) <= 6000 for body in peer.request_bodies))
            messages = json.loads(peer.request_bodies[-1])["messages"]
            history = json.loads(next(message["content"] for message in messages if message["role"] == "tool"))
            self.assertFalse(history["value"]["records"][0]["complete"])

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
            build = ChatEndpointFactory(
                spec=ChatEndpointSpec(configuration_id="fixture", model="fixture", url=peer.origin + "/chat",
                                      api_key="PRIVATE_KEY", reasoning_effort=None),
                trust=peer.trust, retirement_seconds=0.5, request_bytes=32768, response_bytes=8192,
            )
            owner = Interpreter((build,), timeout_seconds=10, context_bytes=16384, secrets=("PRIVATE_KEY",))
            dispatch = AsyncMock(return_value=ToolFinished("prepared"))
            tools = [{"type": "function", "function": {"name": "prepare", "parameters": {
                "type": "object", "properties": {"format": {"enum": ["text"]}}}}}]
            result = await owner.run([], tools, dispatch)
            self.assertEqual(result.value, "prepared")
            self.assertEqual(len(peer.requests), 3)
            self.assertEqual(json.loads(result.observations[0])["turns_started"], 2)
            dispatch.assert_awaited_once_with("prepare", {"format": "text"}, not_after=ANY)
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
            build = ChatEndpointFactory(
                spec=ChatEndpointSpec(configuration_id="fixture", model="fixture", url=peer.origin + "/chat",
                                      api_key="PRIVATE_KEY", reasoning_effort=None),
                trust=peer.trust, proxy_socket=proxy.path, retirement_seconds=0.5,
                request_bytes=32768, response_bytes=8192,
            )
            owner = Interpreter((build,), timeout_seconds=10, context_bytes=16384, secrets=("PRIVATE_KEY",))
            dispatch = AsyncMock(return_value=ToolFinished("prepared"))
            tools = [{"type": "function", "function": {"name": "prepare", "parameters": {"type": "object"}}}]
            result = await owner.run([], tools, dispatch)
            self.assertEqual(result.value, "prepared")
            self.assertEqual(len(peer.requests), 2)
            self.assertEqual(len(proxy.headers), 2)
            dispatch.assert_awaited_once()
            self.assertTrue(owner.retirement_confirmed)
