"""Configured conversations settle tool work and preserve captured grants."""

import asyncio
import base64
import ssl
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, Mock

from provider_core.api_config import https_trust
from provider_core.conversation import ConversationRetirementFailed, ToolFinished, ToolRejected
from provider_core.interpreter_settings import InterpreterSettings
from tests.fakes.conversation import call, endpoint, interpreter_test_limits


def policy():
    return interpreter_test_limits() | {
        "interpretation_budget_seconds": 30,
        "reject_combined_messages_and_tools_over_bytes": 4096,
    }


TOOLS = [{"type": "function", "function": {"name": "prepare", "parameters": {"type": "object"}}}]


def configured(model):
    return InterpreterSettings((lambda **grant: model,), policy=policy(), secrets=())


class InterpreterSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejection_corrects_before_acceptance_and_retirement(self):
        model = endpoint("model", call("bad", "prepare", {"count": 0}), call("good", "prepare", {"count": 1}))
        conversation = configured(model).new_interpreter()
        preparations = []

        async def prepare_input(name, arguments, *, not_after):
            if not (arguments["count"] > 0):
                raise ToolRejected(f"count must be positive; {arguments["count"]=}")

            preparations.append(arguments)

            return ToolFinished("accepted")

        interpretation = await conversation.run([], TOOLS, prepare_input)

        self.assertEqual(interpretation.value, "accepted")
        self.assertEqual(preparations, [{"count": 1}])
        self.assertEqual(len(interpretation.correction_failures), 1)
        self.assertTrue(conversation.retirement_confirmed)
        await conversation.retire()
        self.assertEqual(conversation.accepted_result.value, "accepted")
        self.assertEqual(model.complete.await_count, 2)

    async def test_cancelling_run_waits_for_tool_cleanup_even_when_cancelled_again(self):
        model = endpoint("model", call("one", "prepare", {}))
        conversation = configured(model).new_interpreter()
        (started, cleaning, release) = (asyncio.Event(), asyncio.Event(), asyncio.Event())
        effects = []

        async def prepare_input(name, arguments, *, not_after):
            started.set()

            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                effects.append("tool settled")

        running = asyncio.create_task(conversation.run([], TOOLS, prepare_input))

        await started.wait()
        running.cancel()
        await cleaning.wait()
        running.cancel()
        await asyncio.sleep(0)
        self.assertFalse(running.done())
        self.assertFalse(conversation.retirement_confirmed)
        release.set()

        with self.assertRaises(BaseException):
            await running

        effects.append("source closed")
        self.assertEqual(effects, ["tool settled", "source closed"])
        self.assertTrue(conversation.retirement_confirmed)
        self.assertEqual(model.complete.await_count, 1)

    async def test_external_retirement_joins_dispatch_and_prevents_successor(self):
        model = endpoint("model", call("one", "prepare", {}))
        conversation = configured(model).new_interpreter()
        started = asyncio.Event()

        async def prepare_input(name, arguments, *, not_after):
            started.set()
            await asyncio.Future()

        running = asyncio.create_task(conversation.run([], TOOLS, prepare_input))

        await started.wait()
        await conversation.retire()

        with self.assertRaises(asyncio.CancelledError):
            await running

        with self.assertRaises(RuntimeError):
            await conversation.run([], TOOLS, prepare_input)

        self.assertEqual(model.complete.await_count, 1)

    async def test_work_failure_is_not_reported_again_as_cleanup_failure(self):
        model = endpoint("model", call("one", "prepare", {}))
        failure = RuntimeError("science failed")
        conversation = configured(model).new_interpreter()

        with self.assertRaises(RuntimeError) as caught:
            await conversation.run([], TOOLS, AsyncMock(side_effect=failure))

        self.assertIs(caught.exception, failure)
        await conversation.retire()
        self.assertTrue(conversation.retirement_confirmed)

    async def test_independent_tool_and_model_cleanup_failures_are_preserved(self):
        model = endpoint("model", call("one", "prepare", {}))
        model.retire = AsyncMock(side_effect=RuntimeError("model cleanup failed"))
        model.retirement_confirmed = False
        failure = RuntimeError("science failed")
        conversation = configured(model).new_interpreter()

        with self.assertRaises(BaseExceptionGroup) as caught:
            await conversation.run([], TOOLS, AsyncMock(side_effect=failure))

        self.assertIn(failure, caught.exception.exceptions)
        self.assertFalse(conversation.retirement_confirmed)

    async def test_accepted_preparation_survives_late_model_cleanup_failure(self):
        model = endpoint("model", call("one", "prepare", {}))
        model.retire = AsyncMock(side_effect=RuntimeError("retirement failed"))
        model.retirement_confirmed = False
        conversation = configured(model).new_interpreter()
        prepared = object()
        prepare_input = AsyncMock(return_value=ToolFinished(prepared))

        with self.assertRaises(ExceptionGroup) as caught:
            await conversation.run([], TOOLS, prepare_input)

        self.assertTrue(all(isinstance(failure, ConversationRetirementFailed) for failure in caught.exception.exceptions))
        self.assertIs(conversation.accepted_result.value, prepared)
        prepare_input.assert_awaited_once()

        with self.assertRaises(ConversationRetirementFailed):
            await conversation.retire()

        prepare_input.assert_awaited_once()

    async def test_managed_scientific_failure_waits_for_model_cleanup(self):
        model = endpoint("model", call("one", "prepare", {}))
        (cleaning, release) = (asyncio.Event(), asyncio.Event())
        failure = RuntimeError("scientific process exited")

        async def retire_model():
            cleaning.set()
            await release.wait()

        model.retire = AsyncMock(side_effect=retire_model)
        conversation = configured(model).new_interpreter()
        running = asyncio.create_task(conversation.run([], TOOLS, AsyncMock(side_effect=failure)))

        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            self.assertFalse(running.done())
            self.assertIs(conversation.last_failure, failure)
            self.assertEqual(1, model.complete.await_count)
        finally:
            release.set()

        with self.assertRaises(RuntimeError) as caught:
            await running

        self.assertIs(failure, caught.exception)
        self.assertTrue(conversation.retirement_confirmed)

    async def test_tool_cancellation_propagates_its_original_cleanup_cause(self):
        model = endpoint("model", call("one", "prepare", {}))
        conversation = configured(model).new_interpreter()
        cleanup_failure = OSError("tool cleanup failed")
        interruption = asyncio.CancelledError("tool interrupted")

        async def prepare_input(*args, **kwargs):
            raise interruption from cleanup_failure

        with self.assertRaises(asyncio.CancelledError) as caught:
            await conversation.run([], TOOLS, prepare_input)

        self.assertIs(interruption, caught.exception)
        self.assertIs(cleanup_failure, caught.exception.__cause__)
        self.assertTrue(conversation.retirement_confirmed)

    async def test_requested_stop_does_not_swallow_independent_tool_cleanup_cause(self):
        model = endpoint("model", call("one", "prepare", {}))
        conversation = configured(model).new_interpreter()
        cleanup_failure = OSError("tool cleanup failed")
        started = asyncio.Event()

        async def prepare_input(*args, **kwargs):
            started.set()

            try:
                await asyncio.Future()
            except asyncio.CancelledError as interruption:
                raise interruption from cleanup_failure

        running = asyncio.create_task(conversation.run([], TOOLS, prepare_input))
        await started.wait()
        running.cancel()

        with self.assertRaises(BaseExceptionGroup) as caught:
            await running

        self.assertIn(cleanup_failure, caught.exception.exceptions)
        self.assertTrue(any(isinstance(failure, asyncio.CancelledError) for failure in caught.exception.exceptions))
        self.assertTrue(conversation.retirement_confirmed)
        model.retire.assert_awaited()

    def test_material_transport_order_and_budgets_are_captured_before_visits(self):
        root_certificate = https_trust(None, label="fixture system trust").get_ca_certs(binary_form=True)[0]
        ca_bytes = ssl.DER_cert_to_PEM_cert(root_certificate).encode("ascii")
        material = {"key": b"captured-key\n", "second-key": b"other-key", "ca": ca_bytes}
        reads = []

        def read_material(material_path, **limits):
            reads.append((material_path, limits))

            return material[material_path]

        configuration = {
            "endpoints": [
                {"configuration_id": "primary", "model": "fixture", "url": "https://example.test/v1/chat/completions", "key_file": "key", "ca_file": "ca"},
                {"configuration_id": "secondary", "model": "longer-fixture", "url": "https://second.test/v1/chat/completions", "key_file": "second-key", "ca_file": "ca", "reasoning_effort": "low"},
            ],
            "transport": {"request_bytes": 8192, "response_bytes": 4096, "error_response_bytes": 1024, "retirement_seconds": 2, "proxy_socket": "/tmp/granted-proxy.sock"},
            "policy": policy() | {"reject_combined_messages_and_tools_over_bytes": 8192},
        }

        original = deepcopy(configuration)
        settings = InterpreterSettings.from_config(configuration, read_material=read_material)
        capture_reads = tuple(reads)

        material.update(key=b"replacement", ca=b"invalid replacement CA")
        configuration["policy"]["connection_timeout_seconds"] = 999

        configuration["transport"].update(proxy_socket="/tmp/replacement.sock", request_bytes=1, response_bytes=1, retirement_seconds=99)
        configuration["endpoints"][0]["model"] = "replacement"

        configuration["endpoints"].reverse()
        conversation = settings.new_interpreter()
        carriers = [service(exchange_seconds=7, connect_seconds=3) for service in settings.services]

        self.assertEqual(original["policy"]["connection_timeout_seconds"], conversation.connection_timeout_seconds)
        self.assertEqual(["primary", "secondary"], [carrier.spec.configuration_id for carrier in carriers])
        self.assertEqual(["fixture", "longer-fixture"], [carrier.spec.model for carrier in carriers])
        self.assertEqual(["captured-key", "other-key"], [carrier.spec.api_key for carrier in carriers])
        self.assertEqual(capture_reads, tuple(reads))
        self.assertEqual(4, len(reads))
        self.assertEqual(min(service.context_bytes for service in settings.services), conversation.reject_combined_messages_and_tools_over_bytes)

        for carrier in carriers:
            self.assertEqual("/tmp/granted-proxy.sock", carrier._transport._proxy_socket)
            self.assertEqual(7, carrier._transport._exchange_seconds)
            self.assertEqual(3, carrier._transport._connect_seconds)
            self.assertEqual(2, carrier._transport._retirement_seconds)
            self.assertEqual((8192, 4096, 1024), (carrier._request_bytes, carrier._response_bytes, carrier._error_response_bytes))
            self.assertEqual(ca_bytes, base64.b64decode(carrier._transport._trust["materials"][0]["data"]))
            self.assertNotIn("captured-key", repr(carrier.spec))

        self.assertNotIn("captured-key", repr(settings))

    def test_invalid_configuration_fails_without_starting_a_model(self):
        factory = Mock()

        with self.assertRaises(ValueError):
            InterpreterSettings((factory,), policy=policy() | {"connection_timeout_seconds": 0}, secrets=())

        factory.assert_not_called()

    def test_backoff_is_shared_only_by_explicit_service_identity(self):
        factory = Mock()
        first = InterpreterSettings((factory,), policy=policy(), secrets=())
        second = InterpreterSettings((factory,), policy=policy(), secrets=())
        shared = InterpreterSettings(first.services, policy=policy(), secrets=())

        self.assertIs(shared.services[0], first.services[0])
        self.assertIsNot(second.services[0], first.services[0])
