"""Actual local TLS checks the chat carrier; no external model is contacted."""

import asyncio
import json
import unittest
from dataclasses import replace

from provider_core.chat import ChatEndpoint, ChatEndpointSpec
from provider_core.transport import HttpTransport
from tests.fakes.tls_peer import TlsPeer


class ChatCarrierTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_explicit_request_and_complete_reply_retire_actual_tls_io(self):
        message = {
            "role": "assistant",
            "tool_calls": [{"id": "call:1", "type": "function", "function": {"name": "inspect", "arguments": "{}"}}],
        }
        body = json.dumps({"choices": [{"message": message, "finish_reason": "tool_calls"}]}).encode()
        async with TlsPeer("success", response_body=body) as peer:
            spec = ChatEndpointSpec(
                configuration_id="local-test",
                model="fixture-model",
                url=peer.origin + "/v1/chat/completions",
                api_key="PRIVATE_API_KEY",
                reasoning_effort=None,
            )
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            endpoint = ChatEndpoint(spec, transport=transport, request_bytes=16384, response_bytes=4096)
            self.addAsyncCleanup(endpoint.retire)
            messages = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "SMILES CCl, N=2"}]
            tools = [{"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}]
            observed = await endpoint.complete(messages, tools)
            self.assertEqual(observed, message)
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertTrue(endpoint.retirement_confirmed)
            (request,) = peer.requests
            self.assertEqual(request.split(b"\r\n", 1)[0], b"POST /v1/chat/completions HTTP/1.1")
            headers = {
                name.lower(): value.strip()
                for line in request.split(b"\r\n")[1:]
                if b":" in line
                for name, value in [line.split(b":", 1)]
            }
            self.assertEqual(headers[b"authorization"], b"Bearer PRIVATE_API_KEY")
            self.assertEqual(
                json.loads(peer.request_bodies[0]),
                {
                    "model": "fixture-model",
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "required",
                    "parallel_tool_calls": False,
                    "stream": False,
                },
            )
            self.assertNotIn("PRIVATE_API_KEY", repr(spec))
            self.assertNotIn(peer.origin, repr(spec))
            identity = json.loads(endpoint.identity)
            self.assertEqual(identity["configuration_id"], "local-test")
            self.assertEqual(identity["model"], "fixture-model")
            self.assertNotIn(b"PRIVATE_API_KEY", endpoint.identity)
            self.assertNotIn(peer.origin.encode(), endpoint.identity)
            rotated = ChatEndpoint(
                replace(spec, api_key="ROTATED_KEY"), transport=transport, request_bytes=16384, response_bytes=4096
            )
            self.assertEqual(rotated.identity, endpoint.identity)
            changed = ChatEndpoint(spec, transport=transport, request_bytes=16384, response_bytes=4097)
            self.assertNotEqual(json.loads(changed.identity)["configuration_sha256"], identity["configuration_sha256"])
