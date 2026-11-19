"""One shared transcript carries completed tool effects across model endpoints."""

import json
import unittest
import os

from tests.fakes.conversation import call, endpoint

from provider_core.chat import ChatFailed
from provider_core.conversation import ToolConversation, ToolFinished, ToolRejected, ToolReply


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def _corrected_success(self):
        rejection = ToolRejected("Choose an advertised record.")
        cause = ValueError("PRIVATE parser cause")
        rejection.__cause__ = cause
        model = endpoint("fixture", call("bad", "prepare", {"private": "PRIVATE_ARGUMENT"}), call("good", "prepare", {}))
        conversation = ToolConversation((model,), max_turns=2, secrets=())

        async def dispatch(_name, arguments):
            if arguments:
                raise rejection

            self.assertEqual(conversation.correction_failures, [(1, rejection)])

            return ToolFinished("selected")

        conversation_result = await conversation.run([], [], dispatch)

        self.assertEqual(conversation_result.correction_failures, ((1, rejection),))
        self.assertIs(conversation_result.correction_failures[0][1].__cause__, cause)
        self.assertEqual(json.loads(conversation_result.observation)["corrections"], 1)
        await conversation.retire()

        return conversation_result

    async def test_corrected_success_retains_original_rejection(self):
        await self._corrected_success()

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_corrected_success_omits_reason_and_arguments(self):
        conversation_result = await self._corrected_success()

        self.assertNotIn(b"Choose an advertised record", conversation_result.observation)
        self.assertNotIn(b"PRIVATE", conversation_result.observation)

    async def _success_after_endpoint_failure(self):
        failure = ChatFailed("request_limit", "Earlier request did not fit; not sent.")

        conversation = ToolConversation(
            (endpoint("first", failure), endpoint("second", call("done", "prepare", {}))),
            max_turns=2,
            secrets=()
        )

        async def dispatch(_name, _arguments):
            return ToolFinished("selected")

        conversation_result = await conversation.run([], [], dispatch)

        self.assertEqual(conversation_result.endpoint_failures, ((0, failure),))
        self.assertIsInstance(conversation_result.endpoint_failures, tuple)
        self.assertEqual(conversation_result.value, "selected")
        await conversation.retire()

        return conversation_result

    async def test_success_hands_off_original_endpoint_failures(self):
        await self._success_after_endpoint_failure()

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_success_observation_omits_endpoint_failure_text(self):
        conversation_result = await self._success_after_endpoint_failure()

        self.assertNotIn(b"Earlier request", conversation_result.observation)

    async def test_completed_tool_and_reasoning_survive_fallback_without_repeating_effect(
        self,
    ):
        first = endpoint(
            "first",
            call("prepare-1", "prepare", {}),
            ChatFailed("exchange_failed", "Fixture reply lost."),
        )

        second = endpoint("second", call("submit-1", "submit", {"reference": "owned-reference"}))
        effects = []

        async def dispatch(name, arguments):
            effects.append(name)

            if name == "prepare":
                return ToolReply({"reference": "owned-reference"})

            first.retire.assert_awaited_once()
            self.assertEqual(arguments, {"reference": "owned-reference"})

            return ToolFinished("submitted")

        conversation = ToolConversation((first, second), max_turns=3, secrets=())
        conversation_result = await conversation.run([{"role": "user", "content": "fixture request"}], [], dispatch)

        self.assertEqual(conversation_result.value, "submitted")
        self.assertEqual(effects, ["prepare", "submit"])
        transcript = second.complete.call_args.args[0]

        self.assertEqual(transcript[1]["reasoning_content"], "fixture reasoning")
        self.assertEqual(json.loads(transcript[2]["content"]), {"reference": "owned-reference"})
        observed = json.loads(conversation_result.observation)

        self.assertEqual(observed["turns_started"], 3)

        self.assertEqual(
            [route["endpoint"]["configuration_id"] for route in observed["routes"]],
            ["first", "second"],
        )

        self.assertEqual(observed["routes"][0]["failure"], "exchange_failed")
        self.assertNotIn(b"owned-reference", conversation_result.observation)
        await conversation.retire()
