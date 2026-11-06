"""Successive Jobs must share the prose service's requested postponement.

These installed-SDK tests drive separate Interpreters, not a provider's Job
discovery loop. Consumer tests own the automatic admission/resumption witness.
"""

import asyncio
import json
import unittest

from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
from provider_core.conversation import ToolFinished
from provider_core.interpreter import InterpretationFailed, Interpreter
from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer


class ProseServiceRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_jobs_respect_the_prose_services_retry_after(self):
        loop = asyncio.get_running_loop()
        restored = asyncio.Event()
        unavailable_reply_sent = asyncio.Event()
        requests, tool_calls, interpreters = [], [], []

        async def response(_request, body):
            job = json.loads(body)["messages"][0]["content"]
            requests.append({"job": job, "at": loop.time(), "available": restored.is_set()})
            if restored.is_set():
                peer.response_status, peer.response_headers = 200, None
                return json.dumps({"choices": [{
                    "message": call("prepare", "prepare_scientific_input", {}),
                    "finish_reason": "tool_calls",
                }]}).encode()
            peer.response_status = 503
            peer.response_headers = ((b"Content-Type", b"application/json"), (b"Retry-After", b"1"))
            unavailable_reply_sent.set()
            return b'{"error":{"message":"The prose interpreter is temporarily unavailable."}}'

        async def restore_only_the_prose_service():
            await unavailable_reply_sent.wait()
            # Restoration does not revoke the one-second wait already sent.
            await asyncio.sleep(0.8)
            restored.set()

        async with TlsPeer("success", response_factory=response) as peer:
            service = ChatEndpointFactory(
                spec=ChatEndpointSpec(
                    configuration_id="b4-prose-service", model="local-prose-test-model",
                    url=peer.origin + "/chat", api_key="B4_TEST_CREDENTIAL", reasoning_effort=None,
                ),
                trust=peer.trust, retirement_seconds=0.3, request_bytes=32768, response_bytes=8192,
            )
            restoration = asyncio.create_task(restore_only_the_prose_service())

            async def interpret(job, seconds):
                interpreter = Interpreter((service,), timeout_seconds=seconds,
                                          context_bytes=16384, secrets=("B4_TEST_CREDENTIAL",))
                interpreters.append(interpreter)

                async def prepare(name, arguments, *, not_after):
                    if name != "prepare_scientific_input" or arguments:
                        raise AssertionError("The test prose service proposed an unexpected scientific operation")
                    tool_calls.append(job)
                    return ToolFinished(job)

                tools = [{"type": "function", "function": {
                    "name": "prepare_scientific_input", "parameters": {"type": "object", "properties": {}},
                }}]
                try:
                    return (await interpreter.run([{"role": "user", "content": job}], tools, prepare)).value
                finally:
                    await interpreter.retire()

            try:
                # Both short Jobs have less time than the server's requested wait.
                # The second Job must expire without opening another HTTP exchange.
                for job in ("job:b4-first", "job:b4-second"):
                    with self.assertRaises(InterpretationFailed):
                        await interpret(job, 0.55)
                result = await interpret("job:b4-third", 2)
                await restoration
                active = {task for task in peer.handlers if not task.done()}
                if active:
                    await asyncio.wait(active, timeout=1)
                evidence = json.dumps(requests, sort_keys=True)
                self.assertEqual("job:b4-third", result, msg=evidence)
                self.assertEqual(["job:b4-third"], tool_calls, msg=evidence)
                self.assertTrue(all(owner.retirement_confirmed for owner in interpreters), msg=evidence)
                self.assertFalse(any(not task.done() for task in peer.handlers), msg=evidence)
                self.assertGreaterEqual(
                    requests[1]["at"] - requests[0]["at"], 1,
                    msg="A later Job bypassed the prose service's one-second Retry-After: " + evidence,
                )
                self.assertEqual(["job:b4-first", "job:b4-third"], [request["job"] for request in requests],
                                 msg=evidence)
            finally:
                restoration.cancel()
                await asyncio.gather(restoration, return_exceptions=True)
