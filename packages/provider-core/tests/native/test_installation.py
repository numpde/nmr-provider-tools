"""Scientific workers use installed native helpers without API dependencies."""

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

from provider_core.upload_file import VerifiedUploadFile, UploadFileReadFailed, read_upload_file
from tests.fakes.runner import RunningSupervisor, worker_pid


class NativeInstallationTests(unittest.TestCase):
    def test_native_conversation_dispatches_without_api_dependencies(self):
        from provider_core.conversation import ToolConversation, ToolFinished
        from tests.fakes.conversation import call, endpoint

        async def run():
            model = endpoint("native", call("selected", "prepare", {"record": 1}))
            conversation = ToolConversation((model,), max_turns=1, secrets=())

            async def dispatch(name, arguments):
                self.assertEqual((name, arguments), ("prepare", {"record": 1}))
                return ToolFinished("accepted")

            try:
                self.assertEqual((await conversation.run([], [], dispatch)).value, "accepted")
            finally:
                await conversation.retire()

        asyncio.run(run())

    def test_native_conversation_handoff_returns_control_without_a_model_failure(self):
        from provider_core.conversation import ConversationHandoff, ToolConversation, ToolHandoff, ToolRejected
        from tests.fakes.conversation import call, endpoint

        async def run():
            model = endpoint("native", call("report", "report_interpretation_problem", {}))
            reason = ToolRejected("This model needs to hand the decision back to its caller.")
            conversation = ToolConversation((model,), max_turns=1, secrets=())
            async def dispatch(name, arguments):
                return ToolHandoff(reason)
            try:
                outcome = await conversation.run([], [], dispatch)
                self.assertIsInstance(outcome, ConversationHandoff)
                self.assertIs(outcome.reason, reason)
                self.assertIsNone(conversation.last_failure)
                self.assertEqual(model.complete.await_count, 1)
                model.retire.assert_not_awaited()
            finally:
                await conversation.retire()

        asyncio.run(run())

    def test_native_interpreter_runs_shared_flow_without_api_dependencies(self):
        from provider_core.interpreter import Interpreter
        from provider_core.conversation import ToolFinished
        from tests.fakes.conversation import call, endpoint

        async def run():
            model = endpoint("native", call("selected", "prepare", {}))
            owner = Interpreter((lambda **grant: model,), timeout_seconds=30,
                                context_bytes=16384, secrets=())
            async def dispatch(name, arguments, *, not_after):
                return ToolFinished("accepted")
            tools = [{"type": "function", "function": {"name": "prepare", "parameters": {"type": "object"}}}]
            self.assertEqual((await owner.run([], tools, dispatch)).value, "accepted")
            self.assertTrue(owner.retirement_confirmed)

        asyncio.run(run())

    def test_native_imports_have_no_third_party_dependency_closure(self):
        for name in ("httpx", "httpcore", "http_sf", "cryptography", "tomli_w"):
            self.assertIsNone(importlib.util.find_spec(name), name)
        import provider_core.runner
        import provider_core.runner_protocol
        import provider_core.runner_session
        import provider_core.process_status
        import provider_core.lifetime
        import provider_core.conversation
        import provider_core.interpreter
        import provider_core.contribution
        import provider_core.hosting
        import provider_core.embedded_host
        import provider_core.testing.conformance
        self.assertFalse({
            "provider_core.chat", "provider_core.chat_process", "provider_core.chat_tls",
            "provider_core.chat_wire", "provider_core.chat_worker",
            "provider_core.api", "provider_core.execution", "provider_core.controller",
        }.intersection(sys.modules), msg="Native conversation imports must not acquire model HTTP/process dependencies")
        distribution = importlib.metadata.distribution("provider-core")
        self.assertTrue(distribution.requires)
        self.assertTrue(all('; extra == "api"' in requirement for requirement in distribution.requires))
        self.assertEqual({"provider-core"}, {item.metadata["Name"] for item in importlib.metadata.distributions()})

    def test_native_runner_retires_its_real_child_and_releases_scratch(self):
        runner = RunningSupervisor(operation_seconds=None)
        self.addCleanup(runner.close)
        session = runner.session(startup_seconds=2, retirement_seconds=3)
        self.addCleanup(session.close)
        session.connect()
        self.assertGreater(worker_pid(session.science), 0)
        session.observe_retirement()
        self.assertEqual(0, session.retirement.returncode)
        self.assertTrue(session.retirement.scratch_released)

    def test_native_reader_returns_verified_bytes_and_refuses_later_mutation(self):
        data = b"scientific input"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            path.write_bytes(data)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                receipt = VerifiedUploadFile("job", "upload", len(data), "sha256:" + hashlib.sha256(data).hexdigest(), "source")
                snapshot = read_upload_file(receipt, directory_fd=descriptor, maximum_bytes=64)
                self.assertEqual(data, snapshot.data)
                path.write_bytes(b"X" * len(data))
                with self.assertRaises(UploadFileReadFailed):
                    read_upload_file(receipt, directory_fd=descriptor, maximum_bytes=64)
                self.assertEqual(data, snapshot.data)
            finally:
                os.close(descriptor)
