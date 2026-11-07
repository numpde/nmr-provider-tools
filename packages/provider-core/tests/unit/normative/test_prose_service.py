"""Jobs share service postponement without sharing deadlines or scientific work."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from provider_core.chat_errors import ChatFailed
from provider_core.conversation import ConversationFailed, ConversationRetirementFailed, ToolFinished
from provider_core.interpreter import InterpretationFailed, Interpreter, ProseService
from tests.fakes.conversation import call, endpoint


TOOLS = [{"type": "function", "function": {
    "name": "prepare", "parameters": {"type": "object", "properties": {}},
}}]


# silent-defaults: allow-contract -- The fixture's default Retry-After header requests one second; tests override it to probe alternate server advice.
def unavailable(wait=b"1"):
    return ChatFailed("http_status", "The prose interpretation service is temporarily unavailable.",
                      response=SimpleNamespace(status=503, headers=((b"Retry-After", wait),)))


# silent-defaults: allow-contract -- The unit fixture grants a deliberately short 40-ms interpretation window unless each test requests another one.
def interpreter(*services, seconds=0.04):
    return Interpreter(services, timeout_seconds=seconds, context_bytes=16384, secrets=())


def service_with(*carriers):
    remaining = iter(carriers)
    return ProseService(lambda **grant: next(remaining))


class ProseServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_job_keeps_its_deadline_and_original_service_failure(self):
        original = unavailable()
        first, second = endpoint("same-service", original), endpoint("same-service")
        service = service_with(first, second)
        for owner in (interpreter(service), interpreter(service)):
            with self.assertRaises(InterpretationFailed):
                await asyncio.wait_for(owner.run([], TOOLS, AsyncMock()), timeout=0.2)
            await owner.retire()
        first.complete.assert_awaited_once()
        second.complete.assert_not_awaited()
        waiting_failure = next(failure for _, failure in owner.failures if isinstance(failure, ChatFailed))
        self.assertEqual("service_wait_deadline", waiting_failure.reason)
        self.assertIs(original, waiting_failure.cause)
        self.assertGreater(service.retry_seconds, 0)
        self.assertTrue(owner.retirement_confirmed)

    async def test_stopping_a_waiting_interpretation_does_not_send_or_prepare(self):
        first, waiting = endpoint("same-service", unavailable()), endpoint("same-service")
        service = service_with(first, waiting)
        with self.assertRaises(InterpretationFailed):
            await interpreter(service).run([], TOOLS, AsyncMock())
        owner, prepare = interpreter(service, seconds=10), AsyncMock()
        task = asyncio.create_task(owner.run([], TOOLS, prepare))
        await asyncio.sleep(0)
        await owner.retire()
        with self.assertRaises(ConversationFailed):
            await asyncio.wait_for(task, timeout=0.2)
        waiting.complete.assert_not_awaited()
        prepare.assert_not_awaited()
        self.assertTrue(owner.retirement_confirmed)

    async def test_old_inflight_success_cannot_cancel_a_newer_service_refusal(self):
        received, release = asyncio.Event(), asyncio.Event()
        old = endpoint("same-service")
        refused = endpoint("same-service", unavailable())
        following = endpoint("same-service")

        async def complete(*args, **kwargs):
            received.set()
            await release.wait()
            return call("old-preparation", "prepare", {})

        old.complete.side_effect = complete
        service = service_with(old, refused, following)
        old_owner = interpreter(service, seconds=2)
        old_task = asyncio.create_task(old_owner.run([], TOOLS, AsyncMock(return_value=ToolFinished("old"))))
        try:
            await asyncio.wait_for(received.wait(), timeout=0.2)
            with self.assertRaises(InterpretationFailed):
                await interpreter(service).run([], TOOLS, AsyncMock())
            release.set()
            self.assertEqual("old", (await old_task).value)
            self.assertGreater(service.retry_seconds, 0)
            with self.assertRaises(InterpretationFailed):
                await interpreter(service).run([], TOOLS, AsyncMock())
            following.complete.assert_not_awaited()
        finally:
            release.set()
            await old_owner.retire()
            await asyncio.gather(old_task, return_exceptions=True)

    async def test_independent_binding_and_fallback_still_prepare_once(self):
        failed, waiting = endpoint("same-label", unavailable()), endpoint("same-label")
        healthy = endpoint("same-label", call("healthy-preparation", "prepare", {}))
        unavailable_service = service_with(failed, waiting)
        healthy_service = service_with(healthy)
        with self.assertRaises(InterpretationFailed):
            await interpreter(unavailable_service).run([], TOOLS, AsyncMock())
        prepare = AsyncMock(return_value=ToolFinished("prepared"))
        result = await interpreter(unavailable_service, healthy_service, seconds=0.08).run([], TOOLS, prepare)
        self.assertEqual("prepared", result.value)
        waiting.complete.assert_not_awaited()
        healthy.complete.assert_awaited_once()
        prepare.assert_awaited_once()
        self.assertGreater(unavailable_service.retry_seconds, 0)
        self.assertEqual(0, healthy_service.retry_seconds)

    async def test_retry_uses_one_delay_and_sends_the_current_remaining_budget(self):
        carrier = endpoint("recovering-service", unavailable(b"0"), call("prepare-once", "prepare", {}))
        service = service_with(carrier)
        prepare = AsyncMock(return_value=ToolFinished("prepared"))
        with patch("provider_core.interpreter.random.uniform", return_value=0.03) as jitter:
            result = await interpreter(service, seconds=0.3).run([], TOOLS, prepare)
        self.assertEqual("prepared", result.value)
        jitter.assert_called_once_with(0.25, 0.75)
        self.assertEqual(2, carrier.complete.await_count)
        first, second = carrier.complete.await_args_list
        self.assertEqual(first.kwargs["not_after"], second.kwargs["not_after"])
        first_budget = json.loads(first.args[0][-1]["content"].split(": ", 1)[1])["budget"]
        second_budget = json.loads(second.args[0][-1]["content"].split(": ", 1)[1])["budget"]
        self.assertGreaterEqual(first_budget["model_seconds_remaining"] - second_budget["model_seconds_remaining"], 0.03)
        self.assertEqual(0, service.retry_seconds)
        prepare.assert_awaited_once()

    async def test_authentication_refusal_does_not_become_temporary_unavailability(self):
        denied = ChatFailed("http_status", "Prose service authentication was refused.",
                            response=SimpleNamespace(status=401, headers=((b"Retry-After", b"60"),)))
        carrier = endpoint("protected-service", denied)
        service = service_with(carrier)
        with self.assertRaises(InterpretationFailed):
            await interpreter(service).run([], TOOLS, AsyncMock())
        self.assertEqual(0, service.retry_seconds)
        carrier.complete.assert_awaited_once()

    async def test_unconfirmed_http_cleanup_prevents_retry_and_fallback(self):
        original = unavailable(b"0")
        unsettled, fallback = endpoint("unsettled", original), endpoint("unused")
        unsettled.retirement_confirmed = False
        owner = interpreter(service_with(unsettled), service_with(fallback))
        prepare = AsyncMock()
        with self.assertRaises(ConversationRetirementFailed):
            await owner.run([], TOOLS, prepare)
        unsettled.complete.assert_awaited_once()
        fallback.complete.assert_not_awaited()
        prepare.assert_not_awaited()
        self.assertFalse(owner.retirement_confirmed)
        self.assertTrue(any(failure is original for _, failure in owner.failures))
