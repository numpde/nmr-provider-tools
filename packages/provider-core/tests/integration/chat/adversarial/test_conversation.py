"""Rejected model turns consume the same budget and cannot replay tool effects."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from tests.fakes.conversation import call, endpoint

from provider_core.chat import ChatFailed
from provider_core.conversation import (
    ConversationFailed,
    ConversationRetirementFailed,
    ToolConversation,
    ToolFinished,
    ToolRejected,
    ToolReply,
)


class ConversationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_prior_correction_survives_exhaustion_operation_failure_cancellation_and_cleanup(self):
        for primary in (None, OSError("PRIVATE operation"), asyncio.CancelledError()):
            with self.subTest(primary=type(primary).__name__):
                rejection = ToolRejected("Select another record.")
                previous = ValueError("PRIVATE earlier cause")
                if primary is not None:
                    primary.__cause__ = previous
                model = endpoint("fixture", call("bad", "prepare", {}), call("next", "prepare", {}))
                # silent-defaults: allow-contract -- One turn reaches exhaustion; the two-turn specimen reaches the injected later failure.
                conversation = ToolConversation((model,), max_turns=1 if primary is None else 2, secrets=())
                dispatch = AsyncMock(side_effect=[rejection, primary])
                expected = ConversationFailed if primary is None else type(primary)
                with self.assertRaises(expected) as caught:
                    await conversation.run([], [], dispatch)
                failure = caught.exception
                if primary is not None:
                    self.assertIs(failure, primary)
                    failure = failure.__cause__
                    self.assertIs(failure.cause, previous)
                self.assertEqual(failure.correction_failures, ((1, rejection),))
                model.retire.side_effect = OSError("PRIVATE cleanup")
                with self.assertRaises(ConversationRetirementFailed) as cleanup:
                    await conversation.retire()
                self.assertEqual(cleanup.exception.correction_failures, ((1, rejection),))
                self.assertIs(cleanup.exception.cause, caught.exception)
                model.retire.side_effect = None
                await conversation.retire()

    async def test_exhausted_endpoints_retain_each_original_failure_with_its_index(self):
        first_failure = ChatFailed("request_limit", "Request did not fit; it was not sent.")
        second_failure = ChatFailed("http_status", "Model service returned HTTP 503.")
        conversation = ToolConversation(
            (endpoint("first", first_failure), endpoint("second", second_failure)), max_turns=2, secrets=()
        )
        with self.assertRaises(ConversationFailed) as caught:
            await conversation.run([], [], AsyncMock())
        self.assertEqual(caught.exception.endpoint_failures, ((0, first_failure), (1, second_failure)))
        self.assertIs(caught.exception.cause, second_failure)
        await conversation.retire()

    async def test_prior_endpoint_history_preserves_foreign_tool_failure_object_and_earlier_cause(self):
        first_failure = ChatFailed("request_limit", "Request was not sent.")
        previous = ValueError("PRIVATE prior tool cause")
        primary = OSError("PRIVATE tool operation")
        primary.__cause__ = previous
        models = (endpoint("first", first_failure), endpoint("second", call("prepare", "prepare", {})))
        conversation = ToolConversation(models, max_turns=2, secrets=())
        dispatch = AsyncMock(side_effect=primary)
        with self.assertRaises(OSError) as caught:
            await conversation.run([], [], dispatch)
        self.assertIs(caught.exception, primary)
        context = primary.__cause__
        self.assertIs(type(context), ConversationFailed)
        self.assertIs(context.cause, previous)
        self.assertEqual(context.endpoint_failures, ((0, first_failure),))
        dispatch.assert_awaited_once_with("prepare", {})
        self.assertEqual(models[1].complete.await_count, 1)
        await conversation.retire()

    async def test_cleanup_after_multiple_endpoint_failures_keeps_all_originals(self):
        first_failure = ChatFailed("request_limit", "First request was not sent.")
        second_failure = ChatFailed("exchange_failed", "Second reply was lost.")
        cleanup = OSError("PRIVATE cleanup")
        first, second = endpoint("first", first_failure), endpoint("second", second_failure)
        second.retire.side_effect = cleanup
        conversation = ToolConversation((first, second), max_turns=2, secrets=())
        with self.assertRaises(ConversationRetirementFailed) as caught:
            await conversation.run([], [], AsyncMock())
        self.assertEqual(caught.exception.endpoint_failures, ((0, first_failure), (1, second_failure)))
        self.assertIs(caught.exception.cause, second_failure)
        self.assertEqual(caught.exception.failures, ((1, cleanup),))
        second.retire.side_effect = None
        await conversation.retire()

    async def test_cancellation_after_fallback_keeps_exact_interruption_and_earlier_cause(self):
        first_failure = ChatFailed("request_limit", "Request was not sent.")
        earlier = OSError("PRIVATE earlier cleanup")
        interruption = asyncio.CancelledError()
        interruption.__cause__ = earlier
        first = endpoint("first", first_failure)
        second = endpoint("second", call("prepare", "prepare", {}))
        unused = endpoint("unused", call("unused", "prepare", {}))
        conversation = ToolConversation((first, second, unused), max_turns=3, secrets=())
        dispatch = AsyncMock(side_effect=interruption)
        with self.assertRaises(asyncio.CancelledError) as caught:
            await conversation.run([], [], dispatch)
        self.assertIs(caught.exception, interruption)
        self.assertIs(type(interruption.__cause__), ConversationFailed)
        self.assertIs(interruption.__cause__.cause, earlier)
        self.assertEqual(interruption.__cause__.endpoint_failures, ((0, first_failure),))
        dispatch.assert_awaited_once_with("prepare", {})
        second.complete.assert_awaited_once()
        unused.complete.assert_not_awaited()
        await conversation.retire()

    async def test_bad_arguments_and_multiple_calls_are_corrected_without_dispatch(
        self,
    ):
        for text in (
            '{"x":1,"x":2}',
            '{"x":NaN}',
            '{"x":1e999}',
            "[]",
            "PRIVATE malformed json",
        ):
            with self.subTest(arguments=text):
                malformed = call("bad", "submit", {})
                malformed["tool_calls"][0]["function"]["arguments"] = text
                multiple = call("multi-1", "submit", {})
                multiple["tool_calls"].extend(call("multi-2", "submit", {})["tool_calls"])
                model = endpoint("fixture", malformed, multiple, call("good", "submit", {}))
                dispatch = AsyncMock(return_value=ToolFinished("accepted"))
                conversation = ToolConversation((model,), max_turns=3, secrets=())
                result = await conversation.run([], [], dispatch)
                dispatch.assert_awaited_once_with("submit", {})
                self.assertEqual(json.loads(result.observation)["corrections"], 2)
                self.assertEqual([turn for turn, _failure in result.correction_failures], [1, 2])
                self.assertTrue(all(type(failure) is ToolRejected for _turn, failure in result.correction_failures))
                feedback = [m for m in model.complete.call_args.args[0] if m["role"] == "tool"]
                self.assertEqual([m["tool_call_id"] for m in feedback], ["bad", "multi-1", "multi-2"])
                self.assertNotIn("PRIVATE", json.dumps(feedback))
                await conversation.retire()

    async def test_duplicate_ids_and_exhaustion_do_not_repeat_a_completed_tool(self):
        model = endpoint("fixture", call("same", "prepare", {}), call("same", "prepare", {}))
        dispatch = AsyncMock(return_value=ToolReply({"reference": "retained"}))
        conversation = ToolConversation((model,), max_turns=2, secrets=())
        with self.assertRaises(ConversationFailed):
            await conversation.run([], [], dispatch)
        dispatch.assert_awaited_once()
        await conversation.retire()

    async def test_tool_failure_is_operational_without_feedback_or_fallback(self):
        failure = OSError("PRIVATE reader crash")
        first = endpoint("first", call("prepare", "prepare", {}))
        second = endpoint("second", call("other", "prepare", {}))
        conversation = ToolConversation((first, second), max_turns=4, secrets=())
        with self.assertRaises(OSError) as caught:
            await conversation.run([], [], AsyncMock(side_effect=failure))
        self.assertIs(caught.exception, failure)
        second.complete.assert_not_awaited()
        await conversation.retire()

    async def test_uncertain_retirement_blocks_endpoint_switch_and_keeps_both_causes(
        self,
    ):
        primary = ChatFailed("exchange_failed", "Fixture reply lost.")
        cleanup = OSError("PRIVATE socket cleanup")
        first, second = (
            endpoint("first", primary),
            endpoint("second", call("unused", "submit", {})),
        )
        first.retire.side_effect = cleanup
        conversation = ToolConversation((first, second), max_turns=3, secrets=())
        with self.assertRaises(ConversationRetirementFailed) as caught:
            await conversation.run([], [], AsyncMock())
        self.assertIs(caught.exception.cause, primary)
        self.assertIs(caught.exception.failures[0][1], cleanup)
        second.complete.assert_not_awaited()
        first.retire.side_effect = None
        await conversation.retire()

    async def test_cancellation_never_starts_another_endpoint(self):
        primary = asyncio.CancelledError()
        first, second = (
            endpoint("first", primary),
            endpoint("second", call("unused", "submit", {})),
        )
        conversation = ToolConversation((first, second), max_turns=3, secrets=())
        with self.assertRaises(asyncio.CancelledError) as caught:
            await conversation.run([], [], AsyncMock())
        self.assertIs(caught.exception, primary)
        second.complete.assert_not_awaited()
        await conversation.retire()

    async def test_late_reply_cannot_dispatch_after_retirement(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def late_reply(*_):
            entered.set()
            await release.wait()
            return call("late", "submit", {})

        model = endpoint("fixture")
        model.complete.side_effect = late_reply
        dispatch = AsyncMock(return_value=ToolFinished("too late"))
        conversation = ToolConversation((model,), max_turns=1, secrets=())
        running = asyncio.create_task(conversation.run([], [], dispatch))
        await entered.wait()
        await conversation.retire()
        release.set()
        with self.assertRaisesRegex(ConversationFailed, "retired"):
            await running
        dispatch.assert_not_awaited()

    async def test_later_cleanup_keeps_the_original_tool_failure(self):
        primary, cleanup = OSError("PRIVATE reader"), OSError("PRIVATE cleanup")
        first, second = (
            endpoint("first", call("prepare", "prepare", {})),
            endpoint("second"),
        )
        conversation = ToolConversation((first, second), max_turns=2, secrets=())
        with self.assertRaises(OSError):
            await conversation.run([], [], AsyncMock(side_effect=primary))
        first.retire.side_effect = cleanup
        with self.assertRaises(ConversationRetirementFailed) as caught:
            await conversation.retire()
        self.assertIs(caught.exception.cause, primary)
        self.assertIs(caught.exception.failures[0][1], cleanup)
        second.retire.assert_awaited_once()

    async def test_only_admitted_carrier_fields_enter_following_requests(self):
        message = call("prepare", "prepare", {})
        message["PRIVATE_unknown"] = "PRIVATE value"
        message["tool_calls"][0]["function"]["PRIVATE_extra"] = "PRIVATE value"
        model = endpoint("fixture", message, call("submit", "submit", {}))
        dispatch = AsyncMock(side_effect=(ToolReply({"reference": "owned"}), ToolFinished(None)))
        conversation = ToolConversation((model,), max_turns=2, secrets=())
        await conversation.run([], [], dispatch)
        self.assertNotIn("PRIVATE", json.dumps(model.complete.call_args.args))
        await conversation.retire()

    async def test_secret_values_are_checked_without_rewriting_json_structure(self):
        for secret in ('"', "\\", "model"):
            with self.subTest(secret=secret):
                model = endpoint("fixture", call("submit", "submit", {}))
                conversation = ToolConversation((model,), max_turns=1, secrets=(secret,))
                result = await conversation.run(
                    [{"role": "user", "content": "safe request"}],
                    [],
                    AsyncMock(return_value=ToolFinished(None)),
                )
                identity = json.loads(result.observation)["routes"][0]["endpoint"]
                self.assertEqual(identity["model"], "fixture")
                await conversation.retire()

    async def test_credentials_in_tool_declarations_never_reach_the_endpoint(self):
        model = endpoint("fixture")
        conversation = ToolConversation((model,), max_turns=1, secrets=("PRIVATE key",))
        with self.assertRaisesRegex(ConversationFailed, "configured credential"):
            await conversation.run([], [{"description": "PRIVATE key"}], AsyncMock())
        model.complete.assert_not_awaited()
        await conversation.retire()

    async def test_decoded_argument_credentials_cannot_reach_another_turn_or_endpoint(self):
        for raw in ('{"message":"\\u0050RIVATE_MODEL_KEY"}', '{"\\u0050RIVATE_MODEL_KEY":0}'):
            with self.subTest(arguments=raw):
                message = call("report", "report", {})
                message["tool_calls"][0]["function"]["arguments"] = raw
                first, second = endpoint("first", message), endpoint("second", call("unused", "submit", {}))
                conversation = ToolConversation((first, second), max_turns=3, secrets=("PRIVATE_MODEL_KEY",))
                dispatch = AsyncMock()
                with self.assertRaisesRegex(ConversationFailed, "configured credential"):
                    await conversation.run([], [], dispatch)
                self.assertEqual(first.complete.await_count, 1)
                second.complete.assert_not_awaited()
                dispatch.assert_not_awaited()
                await conversation.retire()

    async def test_fallback_preserves_each_private_failure_without_publishing_its_body(self):
        failures = (
            ChatFailed("exchange_failed", "First reply lost.", cause=OSError("PRIVATE first")),
            ChatFailed("exchange_failed", "Second reply lost.", cause=OSError("PRIVATE second")),
        )
        models = (
            endpoint("first", failures[0]),
            endpoint("second", failures[1]),
            endpoint("third", call("submit", "submit", {})),
        )
        conversation = ToolConversation(models, max_turns=3, secrets=())
        result = await conversation.run([], [], AsyncMock(return_value=ToolFinished(None)))
        await conversation.retire()
        self.assertEqual(conversation.endpoint_failures, [(0, failures[0]), (1, failures[1])])
        self.assertNotIn(b"PRIVATE", result.observation)

    async def test_failed_exchange_uses_the_shared_turn_budget_before_fallback(self):
        failure = ChatFailed("exchange_failed", "Fixture reply lost.")
        first, second = endpoint("first", failure), endpoint("second", call("unused", "submit", {}))
        conversation = ToolConversation((first, second), max_turns=1, secrets=())
        with self.assertRaises(ConversationFailed) as caught:
            await conversation.run([], [], AsyncMock())
        self.assertIs(caught.exception.cause, failure)
        second.complete.assert_not_awaited()
        first.retire.assert_awaited_once()
        await conversation.retire()
