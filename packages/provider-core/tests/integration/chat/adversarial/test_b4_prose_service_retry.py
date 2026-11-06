"""Successive Jobs must share the prose service's requested postponement.

These installed-SDK tests drive separate Interpreters, not a provider's Job
discovery loop. Consumer tests own the automatic admission/resumption witness.
"""

import asyncio
import json
import unittest

from provider_core.chat import ChatEndpointFactory, ChatEndpointSpec
from provider_core.conversation import ToolFinished
from provider_core.interpreter import InterpretationFailed, Interpreter, ProseService
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
            peer.response_headers = ((b"Content-Type", b"application/json"), (b"Retry-After", b"2"))
            unavailable_reply_sent.set()
            return b'{"error":{"message":"The prose interpreter is temporarily unavailable."}}'

        async def restore_only_the_prose_service():
            await unavailable_reply_sent.wait()
            # Restoration does not revoke the two-second wait already sent.
            await asyncio.sleep(1.6)
            restored.set()

        async with TlsPeer("success", response_factory=response) as peer:
            service = ProseService(ChatEndpointFactory(
                spec=ChatEndpointSpec(
                    configuration_id="b4-prose-service", model="local-prose-test-model",
                    url=peer.origin + "/chat", api_key="B4_TEST_CREDENTIAL", reasoning_effort=None,
                ),
                trust=peer.trust, retirement_seconds=0.3, request_bytes=32768, response_bytes=8192,
            ))
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
                # The first Job has enough time to receive the 503 over real TLS
                # but not to wait the full Retry-After before its deadline.
                # The second Job expires without opening another exchange.
                for job, seconds in (("job:b4-first", 2.0), ("job:b4-second", 0.55)):
                    with self.assertRaises(InterpretationFailed):
                        await interpret(job, seconds)
                    if job == "job:b4-first":
                        self.assertEqual(
                            ["job:b4-first"], [request["job"] for request in requests],
                            msg="The first Job's deadline elapsed before the local HTTPS "
                                "service could supply Retry-After; no cross-Job delay "
                                "was actually established: " + json.dumps(requests),
                        )
                try:
                    result = await interpret("job:b4-third", 5)
                except InterpretationFailed as failure:
                    raise AssertionError(
                        "The restored service did not complete the third Job inside "
                        "the test's model budget; compare accepted HTTP exchanges "
                        "and startup/response time: " + json.dumps(requests)
                    ) from failure
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
                    requests[1]["at"] - requests[0]["at"], 2,
                    msg="A later Job bypassed the prose service's two-second Retry-After: " + evidence,
                )
                self.assertEqual(["job:b4-first", "job:b4-third"], [request["job"] for request in requests],
                                 msg=evidence)
            finally:
                restoration.cancel()
                await asyncio.gather(restoration, return_exceptions=True)
