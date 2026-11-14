"""Verify real index reuse and independent analysis, result and instance lifetimes."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import (
    OperationPolicy, NotAdmitted, Outcome, ResultConsumption, ResultState,
    observe_cleanup, observe_completion,
)
from reference_provider import science
from reference_provider import __main__ as demo
from reference_provider.binding import ReferenceMatcher, create_binding


class ReferenceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_analyses_reuse_the_index_while_results_remain_independently_owned(self):
        build = self.enterContext(patch("reference_provider.science.build_index", wraps=science.build_index))
        match = self.enterContext(patch("reference_provider.science.match", wraps=science.match))
        host = EmbeddedHost(
            input_bytes=8192,
            result_bytes=8192,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )
        operation_policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=10,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=4096,
            reject_encoded_result_over_bytes=4096,
        )
        operations = []

        def permitted():
            return None

        try:
            binding = create_binding(host=host)
            call = binding.call(method=ReferenceMatcher.match)
            library = binding.contribution
            async with asyncio.timeout(15):
                for name, values, expected in (
                    ("first", (3, 4, 0, 0), "pattern-a"),
                    ("second", (0, 0, 1, 1), "pattern-b"),
                ):
                    analysis = host.open_analysis(allocations=(binding.allocation,))
                    reservation, = analysis.reservations
                    operation = analysis.submit_method(
                        method=binding.contribution.match,
                        scientific_input=science.VectorInput(values=values),
                        states=(), reservation=reservation, policy=operation_policy,
                    )
                    self.assertNotIsInstance(operation, NotAdmitted)
                    completed = await observe_completion(operation)
                    self.assertIs(library, host.residents[binding.allocation.allocation].value)
                    self.assertIs(Outcome.SUCCEEDED, completed.outcome)
                    self.assertEqual((), completed.failures)
                    payload = await operation.read_result(completed.result)
                    report = call.result_codec.decode(payload=payload)
                    self.assertEqual(expected, report.label)
                    self.assertAlmostEqual(1.0, report.score)
                    operations.append((operation, completed.result, payload))
                    await observe_cleanup(analysis.close())
                    self.assertEqual(1, build.call_count)
                    self.assertIsNotNone(library.loaded_index)

                first_index = match.call_args_list[0].kwargs["index"]
                self.assertIs(first_index, match.call_args_list[1].kwargs["index"])
                for operation, result, payload in operations:
                    self.assertEqual(payload, await operation.read_result(result))
                    received = []

                    async def receive(value):
                        received.append(value)

                    consumption = ResultConsumption(operation=operation, result=result, receive=receive)
                    await consumption.consume()
                    self.assertEqual([payload], received)
                    released = await operation.observe(after=None, wait_seconds=0)
                    self.assertIs(ResultState.RELEASED, released.result_state)
        finally:
            async with asyncio.timeout(15):
                await observe_cleanup(host.retire())
        self.assertIsNone(library.loaded_index)

    async def test_second_analysis_failure_does_not_suppress_the_first_report(self):
        original_match = science.match
        calls = []
        cause = OSError("The second reference query failed")
        output = io.BytesIO()

        def fail_second(*, index, settings, candidate):
            calls.append(candidate)
            if len(calls) == 2:
                raise cause
            return original_match(index=index, settings=settings, candidate=candidate)

        with (
            patch("reference_provider.science.match", side_effect=fail_second),
            patch.object(demo, "sys", SimpleNamespace(stdout=SimpleNamespace(buffer=output))),
        ):
            async with asyncio.timeout(15):
                with self.assertRaises(OSError) as raised:
                    await demo.run_demo()
        self.assertIs(cause, raised.exception)
        report = json.loads(output.getvalue())
        self.assertEqual("pattern-a", report["label"])
        self.assertAlmostEqual(1, report["score"])


class ReferenceCommandTests(unittest.TestCase):
    def test_installed_command_reuses_the_library_outside_its_source_tree(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "reference_provider"],
                cwd=Path(directory), capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        reports = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(["pattern-a", "pattern-b"], [report["label"] for report in reports])
        for report in reports:
            self.assertAlmostEqual(1.0, report["score"])
