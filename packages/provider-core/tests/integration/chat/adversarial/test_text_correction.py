"""Explicit text repair grants no extra calls, effects or private disclosure."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from provider_core.conversation import ConversationFailed, TextCorrection, ToolConversation, ToolFinished, ToolRejected, ToolReply
from tests.fakes.conversation import call, recording_endpoint


class TextCorrectionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_feedback_only_mode_refuses_a_reply_without_tool_calls(self):
        for extra in ({}, {"tool_calls": None}, {"tool_calls": []}):
            with self.subTest(extra=extra):
                model = recording_endpoint("only", {"role": "assistant", "content": "Try again."} | extra)
                conversation = ToolConversation((model,), max_turns=2, secrets=(), text_correction=TextCorrection())
                dispatch = AsyncMock()
                with self.assertRaises(ConversationFailed):
                    await conversation.run([], [], dispatch)
                self.assertEqual(model.complete.await_count, 1)
                dispatch.assert_not_awaited()
                await conversation.retire()

    async def test_argument_byte_limit_applies_before_decoding_and_correction_scrubs_the_text(self):
        oversized = call("bad", "submit", {})
        oversized["tool_calls"][0]["function"]["arguments"] = " " * 40 + "{}"
        model = recording_endpoint("only", oversized, call("done", "submit", {}))
        conversation = ToolConversation(
            (model,), max_turns=2, secrets=(), text_correction=TextCorrection(), argument_bytes=8,
        )
        dispatch = AsyncMock(return_value=ToolFinished("selected"))
        await conversation.run([], [], dispatch)
        self.assertEqual(model.requests[1][0]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertIn("8-byte limit", model.requests[1][1]["content"])
        dispatch.assert_awaited_once_with("submit", {})
        await conversation.retire()

    def test_instruction_requires_exact_nonempty_text(self):
        for content in (7, b"instruction"):
            with self.subTest(content=content), self.assertRaises(TypeError):
                TextCorrection(user_content=content)
        with self.assertRaises(ValueError):
            TextCorrection(user_content="")

    async def test_secret_instruction_is_refused_before_the_first_model_request(self):
        model = recording_endpoint("only", call("unused", "submit", {}))
        conversation = ToolConversation(
            (model,), max_turns=2, secrets=("KEY_SECRET",),
            text_correction=TextCorrection(user_content="Use KEY_SECRET to correct this."),
        )
        with self.assertRaises(ConversationFailed):
            await conversation.run([], [], AsyncMock())
        model.complete.assert_not_awaited()
        await conversation.retire()

    async def test_secret_rejection_never_reaches_feedback_or_a_second_model_request(self):
        model = recording_endpoint("only", call("first", "submit", {}), call("unused", "submit", {}))
        conversation = ToolConversation(
            (model,), max_turns=2, secrets=("KEY_SECRET",),
            text_correction=TextCorrection(user_content="correction-v1\n"),
        )
        with self.assertRaises(ConversationFailed) as caught:
            await conversation.run([], [], AsyncMock(side_effect=ToolRejected("Cannot use KEY_SECRET.")))
        self.assertEqual(1, model.complete.await_count)
        self.assertNotIn("KEY_SECRET", str(caught.exception))
        await conversation.retire()

    async def test_invalid_and_unparsed_arguments_are_erased_before_text_repair(self):
        invalid = call("bad", "submit", {})
        invalid["tool_calls"][0]["function"]["arguments"] = r'{"value":"KEY\u005fSECRET","number":NaN}'
        multiple = call("multi-1", "submit", {"private": "KEY_SECRET"})
        multiple["tool_calls"].extend(call("multi-2", "submit", {"private": "KEY_SECRET"})["tool_calls"])
        model = recording_endpoint("only", invalid, multiple, call("good", "submit", {}))
        conversation = ToolConversation(
            (model,), max_turns=3, secrets=("KEY_SECRET",),
            text_correction=TextCorrection(user_content="correction-v1\n"),
        )
        dispatch = AsyncMock(return_value=ToolFinished("selected"))
        await conversation.run([], [], dispatch)
        transcript = model.requests[2]
        self.assertNotIn("SECRET", json.dumps(transcript))
        for message in transcript:
            if message["role"] == "assistant":
                self.assertTrue(all(item["function"]["arguments"] == "{}" for item in message["tool_calls"]))
        feedback = [message for message in transcript if message["role"] == "tool"]
        self.assertEqual(["bad", "multi-1", "multi-2"], [message["tool_call_id"] for message in feedback])
        self.assertTrue(all(message["content"] for message in feedback))
        dispatch.assert_awaited_once_with("submit", {})
        await conversation.retire()

    async def test_explicit_empty_call_list_remains_a_carrier_failure(self):
        first = recording_endpoint("bad", {"role": "assistant", "tool_calls": []})
        second = recording_endpoint("good", call("done", "submit", {}))
        conversation = ToolConversation(
            (first, second), max_turns=2, secrets=(),
            text_correction=TextCorrection(user_content="correction-v1\n"),
        )
        result = await conversation.run([], [], AsyncMock(return_value=ToolFinished("selected")))
        self.assertEqual(1, len(result.endpoint_failures))
        self.assertEqual((), result.correction_failures)
        self.assertEqual([[]], second.requests)
        first.retire.assert_awaited_once()
        await conversation.retire()

    async def test_plain_repairs_exhaust_the_same_total_budget_without_dispatch(self):
        plain = {"role": "assistant", "content": "Try again."}
        model = recording_endpoint("only", plain, plain, call("unused", "submit", {}))
        conversation = ToolConversation(
            (model,), max_turns=2, secrets=(),
            text_correction=TextCorrection(user_content="correction-v1\n"),
        )
        dispatch = AsyncMock()
        with self.assertRaises(ConversationFailed) as caught:
            await conversation.run([], [], dispatch)
        self.assertEqual(2, model.complete.await_count)
        self.assertEqual([1, 2], [turn for turn, _ in caught.exception.correction_failures])
        dispatch.assert_not_awaited()
        await conversation.retire()

    async def test_reused_identifier_cannot_repeat_a_completed_tool_effect(self):
        model = recording_endpoint("only", call("same", "prepare", {}), call("same", "prepare", {}))
        conversation = ToolConversation(
            (model,), max_turns=2, secrets=(),
            text_correction=TextCorrection(user_content="correction-v1\n"),
        )
        dispatch = AsyncMock(return_value=ToolReply({"reference": "owned"}))
        with self.assertRaises(ConversationFailed):
            await conversation.run([], [], dispatch)
        dispatch.assert_awaited_once_with("prepare", {})
        await conversation.retire()

    async def test_late_plain_reply_after_retirement_cannot_start_a_repair_or_dispatch(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def complete(*_):
            entered.set()
            await release.wait()
            return {"role": "assistant", "content": "Try again."}

        model = recording_endpoint("only")
        model.complete.side_effect = complete
        conversation = ToolConversation(
            (model,), max_turns=2, secrets=(),
            text_correction=TextCorrection(user_content="correction-v1\n"),
        )
        dispatch = AsyncMock()
        original = asyncio.create_task(conversation.run([], [], dispatch))
        await entered.wait()
        await conversation.retire()
        release.set()
        with self.assertRaises(ConversationFailed):
            await original
        self.assertEqual(1, model.complete.await_count)
        dispatch.assert_not_awaited()
