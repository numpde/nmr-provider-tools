"""Caller-granted text corrections retain one owned, bounded transcript."""

import json
import unittest
from unittest.mock import AsyncMock

import provider_core.conversation as conversation_api
from provider_core.conversation import ToolConversation, ToolFinished, ToolRejected
from tests.fakes.conversation import call, recording_endpoint


class TextCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_tool_feedback_does_not_add_a_user_instruction(self):
        model = recording_endpoint("only", call("bad", "submit", {"record": 1}), call("good", "submit", {}))

        conversation = ToolConversation(
            (model,),
            max_turns=2,
            secrets=(),
            text_correction=conversation_api.TextCorrection(),
        )

        await conversation.run([], [], AsyncMock(side_effect=[ToolRejected("Choose another record."), ToolFinished("selected")]))
        (assistant, tool) = model.requests[1]

        self.assertEqual({"record": 1}, json.loads(assistant["tool_calls"][0]["function"]["arguments"]))
        self.assertEqual({"role": "tool", "tool_call_id": "bad", "content": "Choose another record."}, tool)
        await conversation.retire()

    async def test_plain_assistant_repairs_with_one_user_instruction_and_no_tool_reply(self):
        for extra in ({}, {"tool_calls": None}):
            with self.subTest(extra=extra):
                plain = {"role": "assistant", "content": "I need to correct that.", "reasoning_content": "Retained reasoning"}
                model = recording_endpoint("only", plain | extra, call("done", "submit", {}))

                conversation = ToolConversation(
                    (model,),
                    max_turns=2,
                    secrets=(),
                    text_correction=conversation_api.TextCorrection(user_content="correction-v1\n"),
                )

                dispatch = AsyncMock(return_value=ToolFinished("selected"))
                conversation_result = await conversation.run([], [], dispatch)

                self.assertEqual([plain, {"role": "user", "content": "correction-v1\n"}], model.requests[1])
                dispatch.assert_awaited_once_with("submit", {})
                self.assertEqual("selected", conversation_result.value)
                self.assertEqual(2, json.loads(conversation_result.observation)["turns_started"])
                self.assertEqual(1, json.loads(conversation_result.observation)["corrections"])
                await conversation.retire()

    async def _caller_rejection_correction(self):
        diagnostic = (
            "Cannot prepare input: shift_lo='5.801' (type=str).\n"
            "Choose a finite number."
        )

        rejection = ToolRejected(diagnostic)
        rejection.__cause__ = ValueError("PRIVATE scientific admission cause")
        value = {"z": 1, "a": "5.801"}
        model = recording_endpoint("only", call("bad", "submit", value), call("good", "submit", {}))

        conversation = ToolConversation(
            (model,),
            max_turns=2,
            secrets=(),
            text_correction=conversation_api.TextCorrection(user_content="correction-v1\n"),
        )

        conversation_result = await conversation.run([], [], AsyncMock(side_effect=[rejection, ToolFinished("selected")]))

        await conversation.retire()

        return model.requests[1], conversation_result, value, diagnostic, rejection

    async def test_caller_rejection_preserves_safe_arguments_and_exact_diagnostic_then_instruction(self):
        (transcript, conversation_result, value, diagnostic, rejection) = await self._caller_rejection_correction()
        (assistant, tool, user) = transcript

        self.assertEqual("assistant", assistant["role"])
        self.assertEqual("fixture reasoning", assistant["reasoning_content"])
        self.assertEqual(value, json.loads(assistant["tool_calls"][0]["function"]["arguments"]))
        self.assertEqual({"role": "tool", "tool_call_id": "bad", "content": diagnostic}, tool)
        self.assertEqual({"role": "user", "content": "correction-v1\n"}, user)
        self.assertEqual(((1, rejection),), conversation_result.correction_failures)

    @unittest.skip("Privacy policy under review")
    async def test_correction_observation_omits_diagnostics_and_scientific_cause(self):
        (_transcript, conversation_result, _value, diagnostic, _rejection) = await self._caller_rejection_correction()

        self.assertNotIn(diagnostic.encode(), conversation_result.observation)
        self.assertNotIn(b"PRIVATE", conversation_result.observation)

    async def _default_rejection_correction(self):
        model = recording_endpoint("only", call("bad", "submit", {"candidate": 1}), call("good", "submit", {}))
        conversation = ToolConversation((model,), max_turns=2, secrets=())

        await conversation.run([], [], AsyncMock(side_effect=[ToolRejected("Choose another value."), ToolFinished("selected")]))
        await conversation.retire()

        return model.requests[1]

    async def test_default_rejection_uses_json_tool_feedback(self):
        (_assistant, tool) = await self._default_rejection_correction()

        self.assertEqual({"rejected": "Choose another value."}, json.loads(tool["content"]))

    @unittest.skip("Privacy policy under review")
    async def test_default_rejection_scrubs_arguments(self):
        (assistant, _tool) = await self._default_rejection_correction()

        self.assertEqual("{}", assistant["tool_calls"][0]["function"]["arguments"])
