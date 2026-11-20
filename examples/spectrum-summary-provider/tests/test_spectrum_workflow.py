"""Exercise the installed provider's real workflow, including interrupted ownership."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import io
import json
import pathlib
import subprocess
import sys
import shutil
import tomllib

# tomli_w writes TOML; the standard library's tomllib only reads it.
# Tests edit temporary deployment files to exercise real provider startup.
import tomli_w

from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from tests.fakes.deployment import staged_configuration

from provider_core.contribution import JsonDataclassCodec
from provider_core.scientific import operation
from provider_core.embedded_host import InProcessExecutionHost
from provider_core import execution_setup

from provider_core.hosting import (
    AdmissionCode,
    CleanupObservation,
    DispatchState,
    Failure,
    NotAdmitted,
    Outcome,
    ResultConsumption,
    ResultState,
    observe_cleanup,
    observe_completion,
)

from spectrum_provider import science
from spectrum_provider import __main__ as demo
from spectrum_provider import provider as provider_setup
from spectrum_provider.binding import BaselineCorrector


class SpectrumWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.configuration = self.enterContext(staged_configuration(
            provider=provider_setup,
            source=pathlib.Path(__file__).parent.parent / "config/deployments/localhost/provider.toml",
        ))

    async def test_lost_submission_reply_reuses_the_generated_invocation(self):
        (workflow, _, _) = await self.workflow(result_bytes=8192)
        submit = workflow.peak_analysis_executor.submit
        accepted = []
        cause = OSError("The reply was lost after submission")

        def lose_first_reply(*, invocation):
            operation = submit(invocation=invocation)

            accepted.append(operation)

            if len(accepted) == 1:
                raise cause

            return operation

        with patch.object(target=workflow.peak_analysis_executor, attribute="submit", side_effect=lose_first_reply):
            with self.assertRaises(expected_exception=OSError) as raised:
                await workflow.finish()

            self.assertIs(expr1=cause, expr2=raised.exception)
            operation_observation = await workflow.finish()

        self.assertIs(expr1=accepted[0], expr2=accepted[1])
        self.assertIs(expr1=Outcome.SUCCEEDED, expr2=operation_observation.outcome)

    async def workflow(self, *, result_bytes):
        configuration = tomllib.loads(self.configuration.read_text(encoding="utf-8"))
        configuration["execution"]["host"]["result_bytes"] = result_bytes

        self.configuration.write_text(data=tomli_w.dumps(configuration), encoding="utf-8")
        provider = await self.enterAsyncContext(provider_setup.open_provider())

        workflow = provider.create_workflow(
            spectrum=science.Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)),
        )

        self.assertNotIsInstance(obj=workflow.start(), cls=NotAdmitted)

        return (workflow, workflow.peak_analysis_executor, provider.peak_call.result_codec)

    async def test_result_survives_analysis_close_until_consumer_accepts(self):
        (workflow, analysis, codec) = await self.workflow(result_bytes=8192)

        async with asyncio.timeout(10):
            operation_observation = await workflow.finish()

            self.assertIs(Outcome.SUCCEEDED, operation_observation.outcome)
            self.assertIs(ResultState.AVAILABLE, operation_observation.result_state)
            self.assertEqual((), operation_observation.failures)
            await observe_cleanup(analysis.close())
            retained = await workflow.peak_detection_submission.operation.read_result(operation_observation.result)
            report = codec.decode(payload=retained)

            self.assertEqual(
                science.PeakReport(peaks=(science.Peak(position=1, height=3), science.Peak(position=3, height=4))),
                report,
            )

            received = []

            async def accept(payload):
                received.append(payload)

            consumption = ResultConsumption(
                operation=workflow.peak_detection_submission.operation,
                result=operation_observation.result,
                receive=accept,
            )

            await consumption.consume()
            released = await workflow.peak_detection_submission.operation.observe(after=None, wait_seconds=0)

            self.assertEqual([retained], received)
            self.assertIs(ResultState.RELEASED, released.result_state)
            await consumption.consume()
            self.assertEqual([retained], received)

    async def test_consumer_failure_preserves_original_result_and_does_not_recalculate(self):
        with patch("spectrum_provider.science.detect_peaks", wraps=science.detect_peaks) as calculate:
            (workflow, _, _) = await self.workflow(result_bytes=8192)
            failure = OSError("The report queue cannot accept this report")

            async def reject(payload):
                raise failure

            async with asyncio.timeout(10):
                operation_observation = await workflow.finish()
                original = workflow.peak_detection_submission.operation

                failed_consumption = ResultConsumption(
                    operation=original,
                    result=operation_observation.result,
                    receive=reject,
                )

                for _ in range(2):
                    with self.assertRaises(OSError) as raised:
                        await failed_consumption.consume()

                    self.assertIs(failure, raised.exception)

                retained = await original.read_result(operation_observation.result)

                async def accept(payload):
                    self.assertEqual(retained, payload)

                # This test receiver refused before any external effect.
                # The caller explicitly selects a new attempt; the failed owner
                # itself never retries or infers that replay is safe.
                replacement = ResultConsumption(operation=original, result=operation_observation.result, receive=accept)

                await replacement.consume()
                self.assertIs(original, workflow.peak_detection_submission.operation)
                self.assertEqual(1, calculate.call_count)

    async def test_cancelled_consumer_wait_resumes_the_same_receiver_without_science_replay(self):
        entered = asyncio.Event()
        acknowledge = asyncio.Event()
        effects = []

        async def receive(payload):
            effects.append(payload)
            entered.set()
            await acknowledge.wait()

        with patch("spectrum_provider.science.detect_peaks", wraps=science.detect_peaks) as calculate:
            (workflow, _, _) = await self.workflow(result_bytes=8192)

            async with asyncio.timeout(10):
                operation_observation = await workflow.finish()

            consumption = ResultConsumption(
                operation=workflow.peak_detection_submission.operation,
                result=operation_observation.result,
                receive=receive,
            )

            waiter = asyncio.create_task(consumption.consume())

            try:
                async with asyncio.timeout(10):
                    await entered.wait()

                waiter.cancel()

                with self.assertRaises(asyncio.CancelledError):
                    await waiter

                original = consumption.acceptance

                self.assertFalse(original.cancelled())
                acknowledge.set()

                async with asyncio.timeout(10):
                    await consumption.consume()

                self.assertIs(original, consumption.acceptance)
                self.assertEqual(1, len(effects))
                self.assertEqual(1, calculate.call_count)
            finally:
                acknowledge.set()
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                await consumption.wait_for_acceptance()

    async def test_cli_settles_consumption_through_repeated_cancellation_before_teardown(self):
        receiving = asyncio.Event()
        acknowledge = asyncio.Event()
        joining = asyncio.Event()
        events = []
        output = io.BytesIO()

        class HeldConsumption(ResultConsumption):
            def __init__(self, *, operation, result, receive):
                async def held_receive(payload):
                    receiving.set()
                    await acknowledge.wait()
                    await receive(payload)
                    events.append("receiver returned")

                super().__init__(operation=operation, result=result, receive=held_receive)

            async def settle(self):
                joining.set()

                return await super().settle()

        def observed_host(*args, **kwargs):
            host = InProcessExecutionHost(*args, **kwargs)
            original_open = host.open_analysis
            original_retire = host.retire

            def open_analysis(*arguments, **options):
                analysis = original_open(*arguments, **options)
                original_close = analysis.close

                def close():
                    events.append("analysis close")

                    return original_close()

                analysis.close = close

                return analysis

            def retire():
                events.append("host retire")

                return original_retire()

            host.open_analysis = open_analysis
            host.retire = retire

            return host

        with (
            patch.object(demo, "ResultConsumption", HeldConsumption),
            patch.object(execution_setup, "InProcessExecutionHost", side_effect=observed_host),
            patch.object(demo, "sys", SimpleNamespace(stdout=SimpleNamespace(buffer=output))),
        ):
            task = asyncio.create_task(demo.run_demo())

            try:
                async with asyncio.timeout(10):
                    await receiving.wait()

                task.cancel("foreground interrupted")

                async with asyncio.timeout(10):
                    await joining.wait()

                task.cancel("shutdown interrupted")
                loop = asyncio.get_running_loop()

                loop.call_soon(task.cancel, "shutdown interrupted again")
                cancellation_callbacks = asyncio.Event()

                loop.call_soon(cancellation_callbacks.set)
                await cancellation_callbacks.wait()
                self.assertEqual([], events)
                acknowledge.set()

                with self.assertRaises(BaseExceptionGroup):
                    await task

                self.assertLess(events.index("receiver returned"), events.index("analysis close"))
                self.assertLess(events.index("receiver returned"), events.index("host retire"))
                self.assertEqual(2, len(json.loads(output.getvalue())["peaks"]))
            finally:
                acknowledge.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_wait_resumes_the_original_scientific_call(self):
        entered = Event()
        proceed = Event()
        subtract = science.subtract_baseline

        def held(spectrum, settings):
            entered.set()

            if not (baseline_calculation_released := proceed.wait(timeout=(release_timeout_seconds := 5))):
                raise TimeoutError(f"{baseline_calculation_released=} after waiting {release_timeout_seconds=}")

            return subtract(spectrum, settings)

        with patch("spectrum_provider.science.subtract_baseline", side_effect=held) as calculate:
            (workflow, _, _) = await self.workflow(result_bytes=8192)
            original = workflow.baseline_submission.operation
            waiter = asyncio.create_task(workflow.finish())

            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                waiter.cancel()

                with self.assertRaises(asyncio.CancelledError):
                    await waiter

                self.assertIs(original, workflow.start())
            finally:
                proceed.set()

            async with asyncio.timeout(10):
                operation_observation = await workflow.finish()

            self.assertIs(Outcome.SUCCEEDED, operation_observation.outcome)
            self.assertEqual(1, calculate.call_count)

    async def test_downstream_refusal_keeps_intermediate_result_owned(self):
        configuration = tomllib.loads(self.configuration.read_text(encoding="utf-8"))
        configuration["execution"]["stage_capacity"]["peaks"]["result_bytes"] = 2048
        self.configuration.write_text(data=tomli_w.dumps(configuration), encoding="utf-8")
        (workflow, _, _) = await self.workflow(result_bytes=8192)

        async with asyncio.timeout(10):
            refused = await workflow.finish()

            self.assertIsInstance(refused, NotAdmitted)
            operation_observation = await workflow.baseline_submission.operation.observe(after=None, wait_seconds=0)
            retained = await workflow.baseline_submission.operation.read_result(operation_observation.result)

            self.assertTrue(retained)
            self.assertIsNone(workflow.peak_detection_submission.operation)
            self.assertIsNone(workflow.baseline_report_release)

    async def test_intermediate_release_failure_does_not_hide_the_final_result(self):
        (workflow, _, codec) = await self.workflow(result_bytes=8192)
        cause = OSError("Intermediate result release failed")

        release = SimpleNamespace(
            observe=AsyncMock(
                return_value=CleanupObservation(
                    revision=1,
                    settled=False,
                    allowance_expired=False,
                    failures=(Failure("release", cause),),
                )
            )
        )

        async with asyncio.timeout(10):
            with patch.object(workflow.baseline_submission.operation, "release_result", return_value=release):
                operation_observation = await workflow.finish()

            report = codec.decode(payload=await workflow.peak_detection_submission.operation.read_result(operation_observation.result))

            self.assertEqual(2, len(report.peaks))
            self.assertIs(Outcome.SUCCEEDED, operation_observation.outcome)

            with self.assertRaises(OSError) as raised:
                await observe_cleanup(workflow.baseline_report_release)

            self.assertIs(cause, raised.exception)

    async def test_stopping_execution_between_stages_refuses_generation(self):
        (workflow, analysis, _) = await self.workflow(result_bytes=8192)

        async with asyncio.timeout(10):
            await observe_completion(workflow.baseline_submission.operation)
            analysis.request_stop()

            with patch("spectrum_provider.science.detect_peaks", wraps=science.detect_peaks) as calculate:
                operation_observation = await workflow.finish()

                self.assertIsInstance(obj=operation_observation, cls=NotAdmitted)
                self.assertIs(expr1=AdmissionCode.CLOSED, expr2=operation_observation.code)
                self.assertIsNone(obj=workflow.peak_detection_submission.operation)
                self.assertEqual(first=0, second=calculate.call_count)
                self.assertIsNotNone(obj=workflow.baseline_report_release)
                self.assertIs(expr1=operation_observation, expr2=await workflow.finish())
                await observe_cleanup(control=workflow.baseline_report_release)
                self.assertIs(expr1=operation_observation, expr2=await workflow.finish())
                self.assertEqual(first=0, second=analysis.host.result_reserved)


    async def test_successful_science_with_failed_encoding_is_not_replayed(self):
        codec = JsonDataclassCodec(value_type=science.Spectrum, format="test.spectrum.v1")
        cause = OSError("Cannot encode the calculated spectrum")

        class BrokenCodec:
            value_type = codec.value_type
            format = codec.format

            def encode(self, value):
                raise cause

            def decode(self, *, payload):
                return codec.decode(payload=payload)

        class BrokenBaseline(BaselineCorrector):
            @operation(result_codec=BrokenCodec())
            def run(self, *, spectrum: science.Spectrum) -> science.Spectrum:
                return super().run(spectrum=spectrum)

        with (
            patch("spectrum_provider.science.subtract_baseline", wraps=science.subtract_baseline) as calculate,
            patch("spectrum_provider.provider.BaselineCorrector", BrokenBaseline),
        ):
            (workflow, _, _) = await self.workflow(result_bytes=8192)

            async with asyncio.timeout(10):
                operation_observation = await workflow.finish()
                repeated_operation_observation = await workflow.finish()

            self.assertIs(Outcome.SUCCEEDED, operation_observation.outcome)
            self.assertIs(ResultState.ENCODING_FAILED, operation_observation.result_state)
            self.assertIsNone(operation_observation.result)
            self.assertTrue(any((failure.cause is cause) for failure in repeated_operation_observation.failures))
            self.assertIsNone(workflow.peak_detection_submission)
            self.assertEqual(1, calculate.call_count)


class SpectrumCommandTests(unittest.TestCase):
    def test_installed_command_runs_outside_its_source_tree(self):
        with TemporaryDirectory() as directory:
            shutil.copytree(src=pathlib.Path(__file__).parent.parent / "config", dst=pathlib.Path(directory) / "config")

            command_result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "spectrum_provider"],
                cwd=pathlib.Path(directory),
                capture_output=True,
                text=True,
                timeout=30,
            )

        self.assertEqual(0, command_result.returncode, command_result.stderr)

        self.assertEqual(
            {"peaks": [{"position": 1.0, "height": 3.0}, {"position": 3.0, "height": 4.0}]},
            json.loads(command_result.stdout),
        )
