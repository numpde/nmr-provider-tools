"""Exercise the installed provider's real workflow, including interrupted ownership."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.contribution import CallRef
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.hosting import (
    CleanupObservation, DispatchState, ExecutionLimits, Failure, Invocation,
    NotAdmitted, Outcome, ResourceGrant, ResultState,
    observe_cleanup, observe_completion,
)
from spectrum_provider import science
from spectrum_provider.application import PeakWorkflow
from spectrum_provider.binding import create_bindings


class SpectrumWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hosts = []

    async def asyncTearDown(self):
        for host in self.hosts:
            async with asyncio.timeout(10):
                await observe_cleanup(host.retire())

    def workflow(self, *, operation_records, bindings=None, guard=None):
        baseline, peaks = create_bindings() if bindings is None else bindings
        grants = (ResourceGrant(kind="cpu", amount=1, unit="thread", selector="generic"),)
        host = EmbeddedHost(
            (Allocation("baseline", baseline, grants), Allocation("peaks", peaks, grants)),
            limits=HostLimits(
                analysis_records=1, operation_records=operation_records,
                input_bytes=8192, result_bytes=8192, state_values=1,
                progress_records=1, initialization_seconds=5, retirement_seconds=5,
            ),
        )
        self.hosts.append(host)
        analysis = host.open_analysis("test", allocation_names=("baseline", "peaks"))
        reservations = {item.allocation: item for item in analysis.reservations}
        limits = ExecutionLimits(
            queue_seconds=5, execution_seconds=10, cleanup_seconds=5,
            input_bytes=4096, result_bytes=4096,
        )
        baseline_call, = baseline.calls

        def permitted():
            return None

        workflow = PeakWorkflow(
            execution=analysis,
            baseline=Invocation(
                operation_id="baseline",
                target=CallRef(baseline.description.binding_id, baseline.description.binding_revision, "run"),
                payload=baseline_call.input_codec.encode(
                    science.Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)),
                ),
                states=(), reservation=reservations["baseline"], limits=limits,
            ),
            peaks=CallRef(peaks.description.binding_id, peaks.description.binding_revision, "run"),
            peak_reservation=reservations["peaks"], limits=limits,
            dispatch_guard=permitted if guard is None else guard,
        )
        self.assertNotIsInstance(workflow.start(), NotAdmitted)
        return workflow, analysis, peaks.calls[0].result_codec

    async def test_result_survives_analysis_close_until_consumer_accepts(self):
        workflow, analysis, codec = self.workflow(operation_records=2)
        async with asyncio.timeout(10):
            completed = await workflow.finish()
            self.assertIs(Outcome.SUCCEEDED, completed.outcome)
            self.assertIs(ResultState.AVAILABLE, completed.result_state)
            self.assertEqual((), completed.failures)
            await observe_cleanup(analysis.close())
            retained = await workflow.peak_operation.read_result(completed.result)
            report = codec.decode(payload=retained)
            self.assertEqual(
                science.PeakReport(peaks=(science.Peak(position=1, height=3), science.Peak(position=3, height=4))),
                report,
            )
            received = []

            async def accept(payload):
                received.append(payload)

            released = await workflow.deliver(accept)
            self.assertEqual([retained], received)
            self.assertIs(ResultState.RELEASED, released.result_state)
            await workflow.deliver(accept)
            self.assertEqual([retained], received)

    async def test_consumer_failure_preserves_original_result_and_does_not_recalculate(self):
        with patch("spectrum_provider.science.detect_peaks", wraps=science.detect_peaks) as calculate:
            workflow, _, _ = self.workflow(operation_records=2)
            failure = OSError("The report queue cannot accept this report")

            async def reject(payload):
                raise failure

            async with asyncio.timeout(10):
                with self.assertRaises(OSError) as raised:
                    await workflow.deliver(reject)
                self.assertIs(failure, raised.exception)
                original = workflow.peak_operation
                completed = await workflow.finish()
                retained = await original.read_result(completed.result)

                async def accept(payload):
                    self.assertEqual(retained, payload)

                await workflow.deliver(accept)
                self.assertIs(original, workflow.peak_operation)
                self.assertEqual(1, calculate.call_count)

    async def test_cancelled_wait_resumes_the_original_scientific_call(self):
        entered = Event()
        proceed = Event()
        subtract = science.subtract_baseline

        def held(spectrum, settings):
            entered.set()
            if not proceed.wait(5):
                raise TimeoutError("Test did not release the original baseline calculation")
            return subtract(spectrum, settings)

        with patch("spectrum_provider.science.subtract_baseline", side_effect=held) as calculate:
            workflow, _, _ = self.workflow(operation_records=2)
            original = workflow.baseline_operation
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
                completed = await workflow.finish()
            self.assertIs(Outcome.SUCCEEDED, completed.outcome)
            self.assertEqual(1, calculate.call_count)

    async def test_downstream_refusal_keeps_intermediate_result_owned(self):
        workflow, _, _ = self.workflow(operation_records=1)
        async with asyncio.timeout(10):
            refused = await workflow.finish()
            self.assertIsInstance(refused, NotAdmitted)
            completed = await workflow.baseline_operation.observe(after=None, wait_seconds=0)
            retained = await workflow.baseline_operation.read_result(completed.result)
            self.assertTrue(retained)
            self.assertIsNone(workflow.peak_operation)
            self.assertIsNone(workflow.baseline_release)

    async def test_intermediate_release_failure_does_not_hide_the_final_result(self):
        workflow, _, codec = self.workflow(operation_records=2)
        cause = OSError("Intermediate result release failed")
        release = SimpleNamespace(observe=AsyncMock(return_value=CleanupObservation(
            revision=1, settled=False, allowance_expired=False,
            failures=(Failure("release", cause),),
        )))
        async with asyncio.timeout(10):
            with patch.object(workflow.baseline_operation, "release_result", return_value=release):
                completed = await workflow.finish()
            report = codec.decode(payload=await workflow.peak_operation.read_result(completed.result))
            self.assertEqual(2, len(report.peaks))
            self.assertIs(Outcome.SUCCEEDED, completed.outcome)
            with self.assertRaises(OSError) as raised:
                await observe_cleanup(workflow.baseline_release)
            self.assertIs(cause, raised.exception)

    async def test_withdrawal_between_stages_preserves_guard_failure(self):
        revoked = Event()
        cause = PermissionError("Dispatch permission was withdrawn")

        def guarded():
            if revoked.is_set():
                raise cause

        workflow, _, _ = self.workflow(operation_records=2, guard=guarded)
        async with asyncio.timeout(10):
            await observe_completion(workflow.baseline_operation)
            revoked.set()
            with patch("spectrum_provider.science.detect_peaks", wraps=science.detect_peaks) as calculate:
                completed = await workflow.finish()
                self.assertIsNone(completed.outcome)
                self.assertIs(DispatchState.NOT_STARTED, completed.dispatch)
                self.assertTrue(any(failure.cause is cause for failure in completed.failures))
                self.assertEqual(0, calculate.call_count)

    async def test_successful_science_with_failed_encoding_is_not_replayed(self):
        baseline, peaks = create_bindings()
        baseline_call, = baseline.calls
        cause = OSError("Cannot encode the calculated spectrum")

        class BrokenCodec:
            value_type = baseline_call.result_codec.value_type
            format = baseline_call.result_codec.format

            def encode(self, value):
                raise cause

            def decode(self, *, payload):
                return baseline_call.result_codec.decode(payload=payload)

        broken_call = replace(baseline_call, result_codec=BrokenCodec())
        broken_binding = replace(baseline, calls=(broken_call,))
        with patch("spectrum_provider.science.subtract_baseline", wraps=science.subtract_baseline) as calculate:
            workflow, _, _ = self.workflow(operation_records=2, bindings=(broken_binding, peaks))
            async with asyncio.timeout(10):
                completed = await workflow.finish()
                repeated = await workflow.finish()
            self.assertIs(Outcome.SUCCEEDED, completed.outcome)
            self.assertIs(ResultState.ENCODING_FAILED, completed.result_state)
            self.assertIsNone(completed.result)
            self.assertTrue(any(failure.cause is cause for failure in repeated.failures))
            self.assertIsNone(workflow.peak_operation)
            self.assertEqual(1, calculate.call_count)


class SpectrumCommandTests(unittest.TestCase):
    def test_installed_command_runs_outside_its_source_tree(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "spectrum_provider"],
                cwd=Path(directory), capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {"peaks": [{"position": 1.0, "height": 3.0}, {"position": 3.0, "height": 4.0}]},
            json.loads(result.stdout),
        )
