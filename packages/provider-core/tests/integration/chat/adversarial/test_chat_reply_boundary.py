"""Actual child reply truncation and late exit cannot dispatch a model tool."""

import asyncio
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
from provider_core.conversation import ToolFinished
from provider_core.interpreter import InterpretationFailed, Interpreter
from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer


class ModelReplyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def run_fault(self, mode):
        body = json.dumps({"choices": [{"message": call("selected", "prepare", {}),
                                       "finish_reason": "tool_calls"}]}).encode()
        children, endpoints = [], []
        launch = asyncio.create_subprocess_exec

        async def observe_launch(*args, **kwargs):
            child = await launch(*args, **kwargs)
            children.append(child)
            return child

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "reply.json"
            fixture = Path(__file__).resolve().parents[3] / "fakes/chat_reply_fault.py"
            command = (sys.executable, "-I", "-B", str(fixture), mode, str(marker))
            async with TlsPeer("success", response_body=body) as peer:
                factory = ChatEndpointFactory(
                    spec=ChatEndpointSpec(configuration_id="reply-boundary", model="fixture",
                                          url=peer.origin + "/chat", api_key="PRIVATE_FIXTURE_KEY",
                                          reasoning_effort=None),
                    trust=peer.trust, retirement_seconds=0.6,
                    request_bytes=32768, response_bytes=8192,
                )

                def observe_endpoint(**settings):
                    endpoint = factory(**settings)
                    endpoints.append(endpoint)
                    return endpoint

                interpreter = Interpreter((observe_endpoint,), timeout_seconds=10,
                                          context_bytes=16384, secrets=("PRIVATE_FIXTURE_KEY",))
                tools = [{"type": "function", "function": {
                    "name": "prepare", "parameters": {"type": "object", "properties": {}}}}]
                dispatch = AsyncMock(return_value=ToolFinished("prepared"))
                deadline = asyncio.get_running_loop().time() + 3
                with patch("provider_core.chat_process._worker_command", return_value=command), \
                        patch("provider_core.chat_process.asyncio.create_subprocess_exec", observe_launch):
                    running = asyncio.create_task(interpreter.run([], tools, dispatch, not_after=deadline))
                    try:
                        async with asyncio.timeout(2.5):
                            while not marker.exists():
                                if running.done():
                                    self.fail("The child failed before returning its real HTTP reply")
                                await asyncio.sleep(0.005)
                        evidence = json.loads(marker.read_text())
                        self.assertEqual(1, len(peer.requests))
                        self.assertEqual(1, len(children))
                        self.assertEqual(1, len(endpoints))
                        self.assertLess(asyncio.get_running_loop().time(), deadline)
                        self.assertIsNone(children[0].returncode)
                        dispatch.assert_not_awaited()
                        if mode != "exit":
                            self.assertLess(evidence["written"], evidence["length"])
                            children[0].kill()
                        else:
                            self.assertEqual(evidence["length"], evidence["written"])
                        with self.assertRaises(InterpretationFailed):
                            await asyncio.wait_for(running, 4)
                        failure = endpoints[0]._transport.last_failure
                        if mode == "exit":
                            self.assertIsInstance(failure.primary, TimeoutError)
                            self.assertLess(asyncio.get_running_loop().time(), deadline + 1)
                        else:
                            self.assertIsInstance(failure.primary, asyncio.IncompleteReadError)
                            expected = evidence["written"] if mode == "header" else evidence["written"] - 4
                            self.assertEqual(expected, len(failure.primary.partial))
                            self.assertEqual(-signal.SIGKILL, children[0].returncode)
                        dispatch.assert_not_awaited()
                        self.assertTrue(interpreter.retirement_confirmed)
                        self.assertIsNotNone(children[0].returncode)
                        self.assertTrue(children[0].stdin.is_closing())
                        await asyncio.wait_for(peer.closed.wait(), 1)
                    finally:
                        for child in children:
                            if child.returncode is None:
                                child.kill()
                            await child.wait()
                        if not running.done():
                            running.cancel()
                        await asyncio.gather(running, return_exceptions=True)
                        await interpreter.retire()

    async def test_child_killed_during_reply_header_or_payload_dispatches_nothing(self):
        for mode in ("header", "payload"):
            with self.subTest(mode=mode):
                await self.run_fault(mode)

    async def test_complete_reply_and_eof_cannot_extend_original_deadline_for_child_exit(self):
        await self.run_fault("exit")
