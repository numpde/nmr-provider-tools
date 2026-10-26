"""Caller-granted text corrections retain one owned, bounded transcript."""

import json
import unittest
from unittest.mock import AsyncMock

import provider_core.conversation as conversation_api
from provider_core.conversation import ToolConversation, ToolFinished, ToolRejected
from tests.fakes.conversation import call, recording_endpoint


class TextCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_assistant_repairs_with_one_user_instruction_and_no_tool_reply(self):
        for extra in ({}, {"tool_calls": None}):
            with self.subTest(extra=extra):
                plain = {"role": "assistant", "content": "I need to correct that.", "reasoning_content": "Retained reasoning"}
                model = recording_endpoint("only", plain | extra, call("done", "submit", {}))
                conversation = ToolConversation(
                    (model,), max_turns=2, secrets=(),
                    text_correction=conversation_api.TextCorrection(user_content="correction-v1\n"),
                )
                dispatch = AsyncMock(return_value=ToolFinished("selected"))
                result = await conversation.run([], [], dispatch)
                self.assertEqual([plain, {"role": "user", "content": "correction-v1\n"}], model.requests[1])
                dispatch.assert_awaited_once_with("submit", {})
                self.assertEqual("selected", result.value)
                self.assertEqual(2, json.loads(result.observation)["turns_started"])
                self.assertEqual(1, json.loads(result.observation)["corrections"])
                await conversation.retire()

    async def test_caller_rejection_preserves_safe_arguments_and_exact_diagnostic_then_instruction(self):
        diagnostic = "Cannot prepare input: shift_lo='5.801' (type=str).\nChoose a finite number."
        rejection = ToolRejected(diagnostic)
        rejection.__cause__ = ValueError("PRIVATE scientific admission cause")
        value = {"z": 1, "a": "5.801"}
        model = recording_endpoint("only", call("bad", "submit", value), call("good", "submit", {}))
        conversation = ToolConversation(
            (model,), max_turns=2, secrets=(),
            text_correction=conversation_api.TextCorrection(user_content="correction-v1\n"),
        )
        result = await conversation.run([], [], AsyncMock(side_effect=[rejection, ToolFinished("selected")]))
        assistant, tool, user = model.requests[1]
        self.assertEqual("assistant", assistant["role"])
        self.assertEqual("fixture reasoning", assistant["reasoning_content"])
        self.assertEqual(value, json.loads(assistant["tool_calls"][0]["function"]["arguments"]))
        self.assertEqual({"role": "tool", "tool_call_id": "bad", "content": diagnostic}, tool)
        self.assertEqual({"role": "user", "content": "correction-v1\n"}, user)
        self.assertEqual(((1, rejection),), result.correction_failures)
        self.assertNotIn(diagnostic.encode(), result.observation)
        self.assertNotIn(b"PRIVATE", result.observation)
        await conversation.retire()

    async def test_default_rejection_still_scrubs_arguments_and_uses_json_tool_feedback(self):
        model = recording_endpoint("only", call("bad", "submit", {"candidate": 1}), call("good", "submit", {}))
        conversation = ToolConversation((model,), max_turns=2, secrets=())
        await conversation.run([], [], AsyncMock(side_effect=[ToolRejected("Choose another value."), ToolFinished("selected")]))
        assistant, tool = model.requests[1]
        self.assertEqual("{}", assistant["tool_calls"][0]["function"]["arguments"])
        self.assertEqual({"rejected": "Choose another value."}, json.loads(tool["content"]))
        await conversation.retire()
