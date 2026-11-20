"""Verify real index reuse and independent analysis, result and instance lifetimes."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import io
import json
import pathlib
import subprocess
import sys
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from provider_core.embedded_host import InProcessExecutionHost
from provider_core import execution_setup

from provider_core.hosting import (
    NotAdmitted,
    Outcome,
    ResultConsumption,
    ResultState,
    observe_cleanup,
    observe_completion,
)

from reference_provider import science
from reference_provider import __main__ as demo
from reference_provider.binding import ReferenceMatcher
from reference_provider import provider as provider_setup


class ReferenceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = pathlib.Path(self.enterContext(TemporaryDirectory()))
        self.configuration = directory / "provider.toml"

        shutil.copyfile(
            src=pathlib.Path(__file__).parent.parent / "config/deployments/localhost/provider.toml",
            dst=self.configuration,
        )

        self.enterContext(patch.object(target=provider_setup, attribute="PROVIDER_CONFIG_PATH", new=self.configuration))

    async def test_two_analyses_reuse_the_index_while_results_remain_independently_owned(self):
        build = self.enterContext(patch("reference_provider.science.build_index", wraps=science.build_index))
        match = self.enterContext(patch("reference_provider.science.match", wraps=science.match))
        operations = []

        async with provider_setup.open_provider() as provider:
            execution_host = provider.execution_host
            operation_policy = provider.operation_policy
            binding = provider.matcher
            call = binding.call(method=ReferenceMatcher.match)
            library = binding.contribution

            async with asyncio.timeout(15):
                for (query_channels, expected) in (
                    ((3, 4, 0, 0), "pattern-a"),
                    ((0, 0, 1, 1), "pattern-b"),
                ):
                    analysis = execution_host.open_analysis(allocations=frozenset({binding.allocation}))
                    (reservation,) = analysis.reservations

                    operation = analysis.submit_method(
                        method=binding.contribution.match,
                        scientific_input=science.VectorInput(values=query_channels),
                        states={},
                        reservation=reservation,
                        policy=operation_policy,
                    )

                    self.assertNotIsInstance(operation, NotAdmitted)
                    completed = await observe_completion(operation)

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

                for (operation, result_ref, payload) in operations:
                    self.assertEqual(payload, await operation.read_result(result_ref))
                    received = []

                    async def receive(report_payload):
                        received.append(report_payload)

                    consumption = ResultConsumption(operation=operation, result=result_ref, receive=receive)

                    await consumption.consume()
                    self.assertEqual([payload], received)
                    released = await operation.observe(after=None, wait_seconds=0)

                    self.assertIs(ResultState.RELEASED, released.result_state)

        self.assertIsNone(library.loaded_index)

    async def test_second_analysis_failure_does_not_suppress_the_first_report(self):
        original_match = science.match
        calls = []
        cause = OSError("The second reference query failed")
        output = io.BytesIO()
        cleanup_checked = []

        def fail_second(*, index, settings, candidate):
            calls.append(candidate)

            if len(calls) == 2:
                raise cause

            return original_match(index=index, settings=settings, candidate=candidate)

        def observed_host(**options):
            owner = InProcessExecutionHost(**options)
            original_open = owner.open_analysis
            opened = []

            def open_analysis(*, allocations):
                analysis = original_open(allocations=allocations)

                opened.append(analysis)

                if len(opened) == 2:
                    original_close = analysis.close

                    def close():
                        # Report acceptance is independent of the failed query's
                        # cleanup. Do not delay it behind that cleanup's wait.
                        self.assertTrue(expr=bool(output.getvalue()), msg="Deliver the prior report before failed-query cleanup")
                        cleanup_checked.append(True)

                        return original_close()

                    analysis.close = close

                return analysis

            owner.open_analysis = open_analysis

            return owner

        with (
            patch("reference_provider.science.match", side_effect=fail_second),
            patch.object(execution_setup, "InProcessExecutionHost", side_effect=observed_host),
            patch.object(demo, "sys", SimpleNamespace(stdout=SimpleNamespace(buffer=output))),
        ):
            async with asyncio.timeout(15):
                with self.assertRaises(OSError) as raised:
                    await demo.run_demo()

        self.assertIs(cause, raised.exception)
        self.assertEqual([True], cleanup_checked)
        report = json.loads(output.getvalue())

        self.assertEqual("pattern-a", report["label"])
        self.assertAlmostEqual(1, report["score"])


class ReferenceCommandTests(unittest.TestCase):
    def test_installed_command_reuses_the_library_outside_its_source_tree(self):
        with TemporaryDirectory() as directory:
            shutil.copytree(src=pathlib.Path(__file__).parent.parent / "config", dst=pathlib.Path(directory) / "config")

            command_result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "reference_provider"],
                cwd=pathlib.Path(directory),
                capture_output=True,
                text=True,
                timeout=30,
            )

        self.assertEqual(0, command_result.returncode, command_result.stderr)
        reports = [json.loads(line) for line in command_result.stdout.splitlines()]

        self.assertEqual(["pattern-a", "pattern-b"], [report["label"] for report in reports])

        for report in reports:
            self.assertAlmostEqual(1.0, report["score"])
