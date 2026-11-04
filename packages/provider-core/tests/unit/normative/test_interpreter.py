"""One interpretation shares time and evidence while provider effects stay owned."""

import asyncio
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from provider_core.chat_errors import ChatFailed
from provider_core.conversation import ConversationFailed, ConversationRetirementFailed, ToolFinished, ToolRejected, ToolReply
from provider_core.interpreter import Interpreter, InterpretationFailed
from tests.fakes.conversation import call, endpoint


TOOLS = [{"type": "function", "function": {"name": "prepare", "parameters": {
    "type": "object", "properties": {"verbosity": {"type": "string", "enum": ["quiet", "full"]}},
    "required": ["verbosity"]}}}]


def factory(model):
    def build(**grant):
        model.grant = grant
        return model
    return build


def interpreter(*models, timeout=240, context=1024 * 1024):
    return Interpreter(tuple(factory(model) for model in models), timeout_seconds=timeout,
                       context_bytes=context, secrets=("PRIVATE_CREDENTIAL",))


def state(messages):
    return json.loads(messages[0]["content"].split(": ", 1)[1])


class InterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def test_time_rolls_forward_only_after_cleanup_and_windows_do_not_restart(self):
        clock = [100.0]
        deadlines = []
        first = endpoint("fast")
        second = endpoint("strong")

        async def fast(messages, tools, *, not_after):
            deadlines.append(not_after)
            clock[0] += 20
            raise ChatFailed("invalid_response", "No complete model reply.")

        async def cleanup():
            if first.retire.await_count == 1:
                clock[0] += 5

        async def strong(messages, tools, *, not_after):
            deadlines.append(not_after)
            return call("finish", "prepare", {"verbosity": "quiet"})

        first.complete.side_effect, first.retire.side_effect = fast, cleanup
        second.complete.side_effect = strong
        owner = interpreter(first, second)
        with patch("provider_core.interpreter._now", side_effect=lambda: clock[0]):
            result = await owner.run([], TOOLS, AsyncMock(return_value=ToolFinished("ready")))
        self.assertEqual(result.value, "ready")
        self.assertEqual(deadlines, [220, 340])
        self.assertEqual(first.grant, {"exchange_seconds": 120, "connect_seconds": 10})
        self.assertEqual(second.grant["exchange_seconds"], 215)
        self.assertEqual(len(result.observations), 2)
        self.assertTrue(owner.retirement_confirmed)

    async def test_retry_stays_on_same_endpoint_same_turn_after_retirement(self):
        events = []
        failure = ChatFailed("http_status", "Unavailable.",
                             response=SimpleNamespace(status=503, headers=((b"Retry-After", b"1"),)))
        model = endpoint("same", failure, call("finish", "prepare", {"verbosity": "full"}))
        original = model.complete

        async def complete(*args, **kwargs):
            events.append("request")
            return await original(*args, **kwargs)

        model.complete = AsyncMock(side_effect=complete)
        model.retire.side_effect = lambda: events.append("retire")
        dispatch = AsyncMock(return_value=ToolFinished("ready"))
        with patch("provider_core.interpreter.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await interpreter(model).run([], TOOLS, dispatch)
        self.assertEqual(events, ["request", "retire", "request", "retire"])
        sleep.assert_awaited_once_with(1.0)
        self.assertEqual(json.loads(result.observations[0])["turns_started"], 1)
        self.assertEqual(model.complete.await_args_list[0].kwargs, model.complete.await_args_list[1].kwargs)
        dispatch.assert_awaited_once()
        self.assertIs(result.failures[0][1], failure)

    async def test_retry_count_and_retry_after_are_bounded_by_original_window(self):
        for retry_after, attempts in ((b"0", 2), (b"999", 1)):
            with self.subTest(retry_after=retry_after):
                failure = ChatFailed("http_status", "Busy.", response=SimpleNamespace(status=429, headers=((b"Retry-After", retry_after),)))
                bad = endpoint("busy", failure, failure)
                good = endpoint("fallback", call("ok", "prepare", {}))
                with patch("provider_core.interpreter.asyncio.sleep", new=AsyncMock()):
                    await interpreter(bad, good, timeout=20).run([], TOOLS, AsyncMock(return_value=ToolFinished("ready")))
                self.assertEqual(bad.complete.await_count, attempts)

    async def test_permanent_error_skips_retry_and_selected_model_has_fixed_deadline(self):
        first = endpoint("unauthorized", ChatFailed("http_status", "Unauthorized.", response=SimpleNamespace(status=401, headers=())))
        second = endpoint("next", call("read", "inspect", {}), call("finish", "prepare", {}))
        result = await interpreter(first, second).run([], TOOLS, AsyncMock(side_effect=[ToolReply({"x": 1}), ToolFinished("ready")]))
        self.assertEqual(first.complete.await_count, 1)
        self.assertEqual(second.complete.await_args_list[0].kwargs, second.complete.await_args_list[1].kwargs)
        self.assertEqual(result.value, "ready")

    async def test_three_proposal_rejections_escalate_with_original_request_and_retrievable_history(self):
        bad = endpoint("fast", *(call(f"bad-{i}", "prepare", {"verbosity": "invalid"}) for i in range(3)))
        good = endpoint("strong", call("read", "read_interpretation_records", {"records": [{"ref": "record-1"}]}),
                        call("done", "prepare", {"verbosity": "quiet"}))
        original = [{"role": "user", "content": "Use the original source faithfully."}]
        captures = []
        original_complete = good.complete

        async def complete(messages, tools, **kwargs):
            captures.append(deepcopy(messages))
            return await original_complete(messages, tools, **kwargs)

        good.complete = AsyncMock(side_effect=complete)
        async def dispatch(name, args):
            if args["verbosity"] == "invalid":
                raise ToolRejected("Use a supported verbosity; the source remains valid.")
            return ToolFinished(args)

        result = await interpreter(bad, good).run(original, TOOLS, dispatch)
        self.assertEqual(bad.complete.await_count, 3)
        self.assertEqual(captures[0][1:], original)
        self.assertEqual(len(state(captures[0])["history"]["records"]), 3)
        returned = json.loads(captures[1][-1]["content"])["value"]["records"][0]
        self.assertEqual(json.loads(returned["json_text"])["arguments"], {"verbosity": "invalid"})
        self.assertEqual(result.value, {"verbosity": "quiet"})

    async def test_schema_queries_use_actual_schema_and_do_not_dispatch_science(self):
        model = endpoint("fixture", call("query", "describe_input_constraints", {"tool": "prepare", "field": "verbosity"}),
                         call("done", "prepare", {"verbosity": "quiet"}))
        dispatch = AsyncMock(return_value=ToolFinished("ready"))
        await interpreter(model).run([], TOOLS, dispatch)
        reply = json.loads(model.complete.await_args_list[-1].args[0][-1]["content"])["value"]
        self.assertEqual(reply["schema"]["enum"], ["quiet", "full"])
        self.assertTrue(reply["required"])
        dispatch.assert_awaited_once_with("prepare", {"verbosity": "quiet"})

    async def test_dynamic_constraints_remain_provider_owned(self):
        model = endpoint("fixture", call("query", "describe_input_constraints", {"tool": "prepare"}), call("done", "prepare", {}))
        describe = AsyncMock(return_value={"available_sources": ["source-1"]})
        await interpreter(model).run([], TOOLS, AsyncMock(return_value=ToolFinished("ready")), describe_constraints=describe)
        describe.assert_awaited_once_with("prepare", None)
        self.assertEqual(json.loads(model.complete.await_args_list[-1].args[0][-1]["content"])["value"], {"available_sources": ["source-1"]})

    async def test_history_reads_captured_observation_without_repeating_live_inspection(self):
        observation = {"source": "source-1", "spectrum": [1, 2]}
        model = endpoint("fixture", call("inspect", "inspect", {"source": "source-1"}),
                         call("history", "read_interpretation_records", {"records": [{"ref": "record-1"}]}),
                         call("finish", "prepare", {}))
        captures = []
        original = model.complete
        async def complete(messages, tools, **kwargs):
            captures.append(deepcopy(messages))
            if len(captures) == 2:
                observation["spectrum"].append(99)
            return await original(messages, tools, **kwargs)
        model.complete = AsyncMock(side_effect=complete)
        dispatch = AsyncMock(side_effect=[ToolReply(observation), ToolFinished("ready")])
        await interpreter(model).run([], TOOLS, dispatch)
        record = json.loads(captures[2][-1]["content"])["value"]["records"][0]
        self.assertEqual(json.loads(record["json_text"])["value"]["spectrum"], [1, 2])
        self.assertIn("historical", record["validity"])
        self.assertEqual(dispatch.await_count, 2)

    async def test_uncertain_retirement_prevents_retry_and_escalation(self):
        first = endpoint("bad", ChatFailed("http_status", "Busy.", response=SimpleNamespace(status=503, headers=())))
        first.retirement_confirmed = False
        next_model = endpoint("unused")
        with self.assertRaises(ConversationRetirementFailed):
            await interpreter(first, next_model).run([], TOOLS, AsyncMock())
        self.assertEqual(first.complete.await_count, 1)
        next_model.complete.assert_not_awaited()

    async def test_scientific_failure_never_escalates_even_if_its_type_is_conversation_failed(self):
        for failure in (RuntimeError("Scientific failure."), ConversationFailed("Scientific operation stopped.")):
            with self.subTest(failure=type(failure).__name__):
                first = endpoint("first", call("prepare", "prepare", {}))
                second = endpoint("unused")
                owner = interpreter(first, second)
                with self.assertRaises(type(failure)) as caught:
                    await owner.run([], TOOLS, AsyncMock(side_effect=failure))
                self.assertIs(caught.exception, failure)
                second.complete.assert_not_awaited()
                first.retire.assert_not_awaited()
                await owner.retire()
                first.retire.assert_awaited()

    async def test_cancellation_identity_survives_cleanup_failure(self):
        cancellation = asyncio.CancelledError()
        first = endpoint("first", call("prepare", "prepare", {}))
        first.retire.side_effect = OSError("Cleanup failed.")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await interpreter(first).run([], TOOLS, AsyncMock(side_effect=cancellation))
        self.assertIs(caught.exception, cancellation)
        self.assertIsInstance(cancellation.__cause__, ConversationRetirementFailed)
        self.assertIs(cancellation.__cause__.cause, cancellation)

    async def test_expired_job_deadline_never_creates_an_endpoint(self):
        build = AsyncMock()
        owner = Interpreter((build,), timeout_seconds=240, context_bytes=8192, secrets=())
        with self.assertRaises(InterpretationFailed):
            await owner.run([], TOOLS, AsyncMock(), not_after=asyncio.get_running_loop().time() - 1)
        build.assert_not_called()

    async def test_total_turn_limit_bounds_inspections_without_using_rejection_budget(self):
        model = endpoint("fixture", *(call(f"inspect-{i}", "inspect", {}) for i in range(12)))
        dispatch = AsyncMock(return_value=ToolReply({"valid": True}))
        with self.assertRaises(InterpretationFailed):
            await interpreter(model).run([], TOOLS, dispatch)
        self.assertEqual(dispatch.await_count, 12)
        self.assertEqual(state(model.complete.await_args_list[-1].args[0])["budget"]["rejections_remaining"], 3)

    async def test_context_overflow_stops_before_network_or_scientific_dispatch(self):
        model = endpoint("unused")
        dispatch = AsyncMock()
        with self.assertRaises(InterpretationFailed):
            await interpreter(model, context=100).run([], TOOLS, dispatch)
        model.complete.assert_not_awaited()
        dispatch.assert_not_awaited()

    async def test_large_history_read_is_explicitly_partial_and_whole_next_context_still_fits(self):
        first = endpoint("fast", call("inspect", "inspect", {}), ChatFailed("invalid_response", "No usable reply."))
        second = endpoint("strong", call("read", "read_interpretation_records", {"records": [{"ref": "record-1"}]}),
                          call("done", "prepare", {}))
        dispatch = AsyncMock(side_effect=[ToolReply({"text": "x" * 80000}), ToolFinished("ready")])
        captures = []
        original = second.complete
        async def complete(messages, tools, **kwargs):
            captures.append(deepcopy(messages))
            return await original(messages, tools, **kwargs)
        second.complete = AsyncMock(side_effect=complete)
        result = await interpreter(first, second, context=32768).run([], TOOLS, dispatch)
        self.assertEqual(result.correction_failures, ())
        reply = json.loads(captures[1][-1]["content"])["value"]["records"][0]
        self.assertFalse(reply["complete"])
        self.assertEqual(reply["next_cursor"], len(reply["json_text"]))
        self.assertLess(reply["next_cursor"], reply["bytes"])
        self.assertEqual(dispatch.await_count, 2)

    async def test_history_evictions_and_missing_references_are_explicit(self):
        first = endpoint("fast", call("one", "inspect", {}), call("two", "inspect", {}),
                         ChatFailed("invalid_response", "No usable reply."))
        second = endpoint("strong", call("read", "read_interpretation_records", {"records": [{"ref": "record-1"}, {"ref": "record-2"}]}),
                          call("done", "prepare", {}))
        dispatch = AsyncMock(side_effect=[ToolReply({"text": "x" * 600000}), ToolReply({"text": "y" * 600000}), ToolFinished("ready")])
        captures = []
        original = second.complete
        async def complete(messages, tools, **kwargs):
            captures.append(deepcopy(messages))
            return await original(messages, tools, **kwargs)
        second.complete = AsyncMock(side_effect=complete)
        await interpreter(first, second, context=3 * 1024 * 1024).run([], TOOLS, dispatch)
        catalogue = state(captures[0])["history"]
        self.assertEqual(catalogue["unavailable_records"], 1)
        self.assertEqual([record["ref"] for record in catalogue["records"]], ["record-2"])
        reply = json.loads(captures[1][-1]["content"])["value"]["records"]
        self.assertEqual(reply[0], {"ref": "record-1", "status": "unavailable"})
        self.assertTrue(reply[1]["complete"])

    async def test_decoded_credential_in_history_is_never_forwarded_or_retrieved(self):
        model = endpoint("fixture", call("inspect", "inspect", {}))
        with self.assertRaises(InterpretationFailed):
            await interpreter(model).run([], TOOLS, AsyncMock(return_value=ToolReply({"text": "PRIVATE_CREDENTIAL"})))
        self.assertEqual(model.complete.await_count, 1)

    async def test_protocol_corrections_share_the_advertised_rejection_budget(self):
        bad = call("bad", "prepare", {})
        bad["tool_calls"][0]["function"]["arguments"] = "not JSON"
        model = endpoint("fixture", bad, call("done", "prepare", {}))
        captures = []
        original = model.complete
        async def complete(messages, tools, **kwargs):
            captures.append(deepcopy(messages))
            return await original(messages, tools, **kwargs)
        model.complete = AsyncMock(side_effect=complete)
        await interpreter(model).run([], TOOLS, AsyncMock(return_value=ToolFinished("ready")))
        self.assertEqual(state(captures[1])["budget"]["rejections_remaining"], 2)

    async def test_retire_during_a_model_visit_cannot_open_a_successor(self):
        entered, released = asyncio.Event(), asyncio.Event()
        first, second = endpoint("active"), endpoint("unused")
        async def complete(*args, **kwargs):
            entered.set()
            await released.wait()
            return call("late", "prepare", {})
        first.complete.side_effect = complete
        first.retire.side_effect = released.set
        dispatch = AsyncMock()
        owner = interpreter(first, second)
        running = asyncio.create_task(owner.run([], TOOLS, dispatch))
        await entered.wait()
        await owner.retire()
        with self.assertRaises(ConversationFailed):
            await running
        self.assertFalse(hasattr(second, "grant"))
        dispatch.assert_not_awaited()

    async def test_late_model_reply_cannot_start_preparation_after_allocation(self):
        clock = [100.0]
        first, second = endpoint("late"), endpoint("next", call("ok", "prepare", {}))
        async def complete(*args, not_after, **kwargs):
            clock[0] = not_after + 1
            return call("too-late", "prepare", {})
        first.complete.side_effect = complete
        dispatch = AsyncMock(return_value=ToolFinished("ready"))
        with patch("provider_core.interpreter._now", side_effect=lambda: clock[0]):
            await interpreter(first, second).run([], TOOLS, dispatch)
        dispatch.assert_awaited_once_with("prepare", {})
        self.assertEqual(first.complete.await_count, 1)
        self.assertEqual(second.complete.await_count, 1)

    async def test_retire_during_retry_backoff_cannot_resend_after_confirmed_retirement(self):
        sleeping, released = asyncio.Event(), asyncio.Event()
        failure = ChatFailed("http_status", "Busy.", response=SimpleNamespace(status=503, headers=()))
        first, second = endpoint("busy", failure, call("late", "prepare", {})), endpoint("unused")
        async def backoff(seconds):
            sleeping.set()
            await released.wait()
        owner = interpreter(first, second)
        with patch("provider_core.interpreter.asyncio.sleep", side_effect=backoff):
            running = asyncio.create_task(owner.run([], TOOLS, AsyncMock()))
            await sleeping.wait()
            await owner.retire()
            self.assertTrue(owner.retirement_confirmed)
            released.set()
            with self.assertRaises(ConversationFailed):
                await running
        self.assertEqual(first.complete.await_count, 1)
        self.assertFalse(hasattr(second, "grant"))

    async def test_accepted_preparation_is_not_replayed_if_it_finishes_after_model_window(self):
        clock = [100.0]
        first, second = endpoint("first", call("prepare", "prepare", {})), endpoint("unused")
        async def dispatch(name, arguments):
            clock[0] += 200
            return ToolFinished("prepared")
        with patch("provider_core.interpreter._now", side_effect=lambda: clock[0]):
            result = await interpreter(first, second).run([], TOOLS, dispatch)
        self.assertEqual(result.value, "prepared")
        self.assertFalse(hasattr(second, "grant"))

    async def test_prior_model_failure_remains_attached_to_later_scientific_failure(self):
        earlier = ChatFailed("invalid_response", "Incomplete reply.")
        first = endpoint("first", earlier)
        second = endpoint("second", call("prepare", "prepare", {}))
        scientific = RuntimeError("Scientific operation failed.")
        with self.assertRaises(RuntimeError) as caught:
            await interpreter(first, second).run([], TOOLS, AsyncMock(side_effect=scientific))
        self.assertIs(caught.exception, scientific)
        context = scientific.__cause__
        self.assertIsInstance(context, ConversationFailed)
        self.assertTrue(any(cause is earlier for _, cause in context.endpoint_failures))

    async def test_exhaustion_exposes_original_failures_to_existing_protected_projection(self):
        from provider_core.failure_projection import owned_details, owned_relationships
        model = endpoint("first", ChatFailed("invalid_response", "Incomplete reply."))
        with self.assertRaises(InterpretationFailed) as caught:
            await interpreter(model).run([], TOOLS, AsyncMock())
        self.assertIn("message", owned_details(caught.exception))
        relationships = tuple(owned_relationships(caught.exception))
        self.assertTrue(any(isinstance(cause, ChatFailed) for _, cause in relationships))

    async def test_provider_crash_propagates_before_blocked_cleanup_with_exact_owner_retained(self):
        entered, release = asyncio.Event(), asyncio.Event()
        first = endpoint("first", call("prepare", "prepare", {}))
        async def blocked_cleanup():
            entered.set()
            await release.wait()
        first.retire.side_effect = blocked_cleanup
        crash = RuntimeError("Provider-established native crash.")
        owner = interpreter(first)
        async with asyncio.timeout(1):
            with self.assertRaises(RuntimeError) as caught:
                await owner.run([], TOOLS, AsyncMock(side_effect=crash))
        self.assertIs(caught.exception, crash)
        self.assertFalse(entered.is_set())
        cleanup = asyncio.create_task(owner.retire())
        await entered.wait()
        self.assertFalse(cleanup.done())
        release.set()
        await cleanup

    async def test_protected_protocol_correction_causes_survive_success_and_retirement(self):
        malformed = call("malformed", "prepare", {})
        malformed["tool_calls"][0]["function"]["arguments"] = "not JSON"
        model = endpoint("fixture", malformed, call("done", "prepare", {}))
        owner = interpreter(model)
        result = await owner.run([], TOOLS, AsyncMock(return_value=ToolFinished("ready")))
        self.assertEqual(len(result.correction_failures), 1)
        visit, turn, failure = result.correction_failures[0]
        self.assertEqual((visit, turn), (1, 1))
        self.assertIsInstance(failure, ToolRejected)
        await owner.retire()
        self.assertEqual(owner.correction_failures, result.correction_failures)

    async def test_unverified_model_input_problem_advances_without_finishing_or_invalidating_job(self):
        from provider_core.interpreter import ModelReportedInputProblem
        first = endpoint("first", call("claim", "report_input_problem", {"detail": "Cannot encode this source."}))
        second = endpoint("second", call("prepare", "prepare", {}))
        async def dispatch(name, arguments):
            if name == "report_input_problem":
                raise ModelReportedInputProblem("The model could not interpret the source; invalidity is not established.")
            return ToolFinished("prepared")
        result = await interpreter(first, second).run([], TOOLS, dispatch)
        self.assertEqual(result.value, "prepared")
        self.assertEqual(first.complete.await_count, 1)
        self.assertEqual(second.complete.await_count, 1)
        self.assertEqual(result.correction_failures, ())
        catalogue = state(second.complete.await_args.args[0])["history"]
        self.assertEqual(catalogue["records"][0]["kind"], "unverified_input_problem")
        first.retire.assert_awaited()

    async def test_endpoint_construction_time_consumes_its_existing_model_window(self):
        clock = [100.0]
        model = endpoint("fixture", call("done", "prepare", {}))

        def build(**grant):
            clock[0] += 20
            return model

        owner = Interpreter((build,), timeout_seconds=240, context_bytes=16384, secrets=())
        with patch("provider_core.interpreter._now", side_effect=lambda: clock[0]):
            result = await owner.run(
                [], TOOLS, AsyncMock(return_value=ToolFinished("ready")), not_after=135,
            )
        self.assertEqual(result.value, "ready")
        self.assertEqual(model.complete.await_args.kwargs["not_after"], 135)
        self.assertEqual(
            state(model.complete.await_args.args[0])["budget"]["model_seconds_remaining"], 15,
        )

    async def test_history_cursor_resumes_the_same_captured_json_without_reinspection(self):
        model = endpoint(
            "fixture",
            call("inspect", "inspect", {}),
            call("full", "read_interpretation_records", {"records": [{"ref": "record-1"}]}),
            call("tail", "read_interpretation_records", {"records": [{"ref": "record-1", "cursor": 17}]}),
            call("done", "prepare", {}),
        )
        captures = []
        original = model.complete

        async def complete(messages, tools, **kwargs):
            captures.append(deepcopy(messages))
            return await original(messages, tools, **kwargs)

        model.complete = AsyncMock(side_effect=complete)
        observation = {"text": 'Quoted "spectrum" and Unicode: λ'}
        dispatch = AsyncMock(side_effect=[ToolReply(observation), ToolFinished("ready")])
        await interpreter(model).run([], TOOLS, dispatch)
        full = json.loads(captures[2][-1]["content"])["value"]["records"][0]
        tail = json.loads(captures[3][-1]["content"])["value"]["records"][0]
        self.assertEqual(tail["cursor"], 17)
        self.assertTrue(tail["complete"])
        self.assertNotIn("next_cursor", tail)
        self.assertEqual(full["json_text"][:17] + tail["json_text"], full["json_text"])
        self.assertEqual(json.loads(full["json_text"])["value"], observation)
        self.assertEqual(dispatch.await_count, 2)

    async def test_provider_argument_normalization_does_not_rewrite_captured_history(self):
        model = endpoint(
            "fixture",
            call("inspect", "inspect", {"source": "source-1", "selection": [1, 2]}),
            call("history", "read_interpretation_records", {"records": [{"ref": "record-1"}]}),
            call("done", "prepare", {}),
        )
        captures = []
        original = model.complete

        async def complete(messages, tools, **kwargs):
            captures.append(deepcopy(messages))
            return await original(messages, tools, **kwargs)

        async def dispatch(name, arguments):
            if name == "inspect":
                arguments.pop("source")
                arguments["selection"].append(3)
                return ToolReply({"rows": 2})
            return ToolFinished("ready")

        model.complete = AsyncMock(side_effect=complete)
        await interpreter(model).run([], TOOLS, dispatch)
        record = json.loads(captures[2][-1]["content"])["value"]["records"][0]
        self.assertEqual(
            json.loads(record["json_text"])["arguments"],
            {"source": "source-1", "selection": [1, 2]},
        )

    async def test_history_pagination_budgets_escaped_payload_and_current_assistant_message(self):
        for text in ('"\\' * 40000, "λ" * 40000):
            with self.subTest(escape=text[0]):
                first = endpoint("first", call("inspect", "inspect", {}))
                read = call("read", "read_interpretation_records", {"records": [{"ref": "record-1"}]})
                read["reasoning_content"] = "model reasoning " * 700
                second = endpoint("second", read, call("done", "prepare", {}))
                dispatch = AsyncMock(side_effect=[ToolReply({"text": text}), ToolFinished("ready")])
                result = await interpreter(first, second, context=32768).run([], TOOLS, dispatch)
                self.assertEqual(result.value, "ready")
                self.assertEqual(second.complete.await_count, 2)
                final_messages, final_tools = second.complete.await_args.args
                size = len(json.dumps({"messages": final_messages, "tools": final_tools},
                                      ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii"))
                self.assertLessEqual(size, 32768)
                self.assertEqual(dispatch.await_count, 2)

    async def test_provider_constraint_reader_failure_propagates_without_model_fallback(self):
        first = endpoint("first", call("query", "describe_input_constraints", {"tool": "prepare"}))
        second = endpoint("unused", call("unexpected", "prepare", {}))
        failure = ConversationFailed("Provider constraints are unavailable.")
        owner = interpreter(first, second)
        with self.assertRaises(ConversationFailed) as caught:
            await owner.run([], TOOLS, AsyncMock(return_value=ToolFinished("unexpected fallback")),
                            describe_constraints=AsyncMock(side_effect=failure))
        self.assertIs(caught.exception, failure)
        self.assertFalse(hasattr(second, "grant"))
        await owner.retire()

    async def test_unknown_path_below_boolean_schema_gets_correction_instead_of_provider_failure(self):
        tools = [{"type": "function", "function": {"name": "prepare", "parameters": {
            "type": "object", "properties": {"opaque": True}}}}]
        model = endpoint(
            "fixture",
            call("query", "describe_input_constraints", {"tool": "prepare", "field": "opaque.anything"}),
            call("done", "prepare", {}),
        )
        result = await interpreter(model).run([], tools, AsyncMock(return_value=ToolFinished("ready")))
        self.assertEqual(result.value, "ready")
        self.assertEqual(len(result.correction_failures), 1)
        self.assertIsInstance(result.correction_failures[0][2], ToolRejected)

    async def test_accepted_preparation_remains_owned_when_model_retirement_fails(self):
        prepared = object()
        model = endpoint("fixture", call("prepare", "prepare", {}))
        model.retire.side_effect = OSError("Model cleanup failed.")
        owner = interpreter(model)
        dispatch = AsyncMock(return_value=ToolFinished(prepared))
        with self.assertRaises(ConversationRetirementFailed):
            await owner.run([], TOOLS, dispatch)
        self.assertIs(owner.accepted_result.value, prepared)
        self.assertIsInstance(owner.last_failure, ConversationRetirementFailed)
        model.retire.side_effect = None
        await owner.retire()
        self.assertIs(owner.accepted_result.value, prepared)
        dispatch.assert_awaited_once()

    async def test_partial_history_page_reserves_status_for_later_batch_records(self):
        first = endpoint(
            "first", call("small", "inspect", {"source": "small"}),
            call("large", "inspect", {"source": "large"}),
        )
        second = endpoint(
            "second",
            call("batch", "read_interpretation_records", {
                "records": [{"ref": "record-2"}, {"ref": "record-1"}],
            }),
            call("done", "prepare", {}),
        )
        dispatch = AsyncMock(side_effect=[
            ToolReply({"rows": 2}), ToolReply({"text": "x" * 80000}), ToolFinished("ready"),
        ])
        result = await interpreter(first, second, context=32768).run([], TOOLS, dispatch)
        self.assertEqual(result.correction_failures, ())
        records = json.loads(second.complete.await_args.args[0][-1]["content"])["value"]["records"]
        self.assertEqual([record["ref"] for record in records], ["record-2", "record-1"])
        self.assertFalse(records[0]["complete"])
        self.assertIn(records[1]["status"], {"returned", "deferred"})
        self.assertEqual(dispatch.await_count, 3)
