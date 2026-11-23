"""Rejoin original stages and settle cancellation without replaying science."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from functools import partial
import json
import pathlib
import shutil
import subprocess
import sys
import tomllib

import tomli_w
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from provider_core.payload import JsonDataclassCodec
from provider_core.hosting import CapacityUnavailable, settle_cleanup
from provider_core.scientific import operation
from provider_core.scientific_invocation import open_invocation
from tests.fakes.deployment import staged_configuration

from spectrum_provider import science
from spectrum_provider import provider as provider_setup
from spectrum_provider.binding import BaselineCorrector


class SpectrumWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.configuration = self.enterContext(
            staged_configuration(
                provider=provider_setup,
                source=pathlib.Path(__file__).parent.parent / "config/deployments/localhost/provider.toml",
            ),
        )

        self.spectrum = science.Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1))

    async def test_cancelled_wait_after_either_stage_dispatch_rejoins_without_replay(self):
        for held_stage in ("subtract_baseline", "detect_peaks"):
            with self.subTest(stage=held_stage):
                entered = Event()
                release = Event()
                calculate = getattr(science, held_stage)

                def held(*args, **kwargs):
                    entered.set()

                    if not (released := release.wait(timeout=5)):
                        raise TimeoutError(f"{released=} should become true before the stage allowance expires")

                    return calculate(*args, **kwargs)

                with (
                    patch.object(science, "subtract_baseline", wraps=science.subtract_baseline) as baseline,
                    patch.object(science, "detect_peaks", wraps=science.detect_peaks) as peaks,
                ):
                    (baseline if held_stage == "subtract_baseline" else peaks).side_effect = held

                    async with provider_setup.open_provider() as provider:
                        async with open_invocation(partial(provider.summarize, spectrum=self.spectrum)) as invocation:
                            waiter = asyncio.create_task(invocation.wait())

                            try:
                                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                                waiter.cancel()

                                with self.assertRaises(asyncio.CancelledError):
                                    await waiter
                            finally:
                                release.set()

                            async with asyncio.timeout(10):
                                report = await invocation.wait()

                                self.assertIs(report, await invocation.wait())

                            self.assertEqual([3.0, 4.0], [peak.height for peak in report.peaks])
                            self.assertEqual(1, baseline.call_count)
                            self.assertEqual(1, peaks.call_count)
                            corrected = peaks.call_args.kwargs["spectrum"]

                            self.assertEqual((0, 3, 1, 4, 0), corrected.intensities)

    async def test_owner_close_settles_running_stage_and_prevents_later_stage(self):
        for held_stage in ("subtract_baseline", "detect_peaks"):
            with self.subTest(stage=held_stage):
                entered = Event()
                release = Event()
                stopping = asyncio.Event()
                calculate = getattr(science, held_stage)

                def held(*args, **kwargs):
                    entered.set()

                    if not (released := release.wait(timeout=5)):
                        raise TimeoutError(f"{released=} should become true before the stage allowance expires")

                    return calculate(*args, **kwargs)

                with (
                    patch.object(science, "subtract_baseline", wraps=science.subtract_baseline) as baseline,
                    patch.object(science, "detect_peaks", wraps=science.detect_peaks) as peaks,
                ):
                    (baseline if held_stage == "subtract_baseline" else peaks).side_effect = held

                    async with provider_setup.open_provider() as provider:
                        stage_call = provider.baseline if held_stage == "subtract_baseline" else provider.peaks
                        original_open = stage_call.host.open_analysis

                        def open_analysis(**options):
                            analysis = original_open(**options)
                            original_close = analysis.close

                            def close():
                                stopping.set()

                                return original_close()

                            analysis.close = close

                            return analysis

                        with patch.object(stage_call.host, "open_analysis", side_effect=open_analysis):
                            async with open_invocation(partial(provider.summarize, spectrum=self.spectrum)) as invocation:
                                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                                closer = asyncio.create_task(invocation.close())

                                try:
                                    async with asyncio.timeout(10):
                                        await stopping.wait()

                                    self.assertFalse(closer.done())
                                finally:
                                    release.set()

                                async with asyncio.timeout(10):
                                    await closer

                                self.assertEqual(1, baseline.call_count)
                                self.assertEqual(int(held_stage == "detect_peaks"), peaks.call_count)

                        # Settled analyses leave both loaded stages reusable
                        report = await provider.summarize(spectrum=self.spectrum)

                        self.assertEqual(2, len(report.peaks))

    async def test_delivery_failure_keeps_typed_report_without_recalculating(self):
        with (
            patch.object(science, "subtract_baseline", wraps=science.subtract_baseline) as baseline,
            patch.object(science, "detect_peaks", wraps=science.detect_peaks) as peaks,
        ):
            async with provider_setup.open_provider() as provider:
                async with open_invocation(partial(provider.summarize, spectrum=self.spectrum)) as invocation:
                    report = await invocation.wait()
                    failure = OSError("Report receiver refused before acceptance")

                    def receive(report):
                        self.assertEqual(2, len(report.peaks))

                        raise failure

                    with self.assertRaises(OSError) as raised:
                        receive(report)

                    self.assertIs(failure, raised.exception)
                    self.assertIs(report, await invocation.wait())
                    self.assertEqual(1, baseline.call_count)
                    self.assertEqual(1, peaks.call_count)
                    self.assertEqual(0, provider.baseline.host.result_reserved)
                    self.assertEqual(0, provider.peaks.host.result_reserved)

    async def test_peak_admission_retry_keeps_corrected_input_without_repeating_baseline(self):
        configuration = tomllib.loads(self.configuration.read_text(encoding="utf-8"))
        configuration["execution"]["error_policy"] = {"max_attempts": 2, "backoff_initial_seconds": 0.001}

        self.configuration.write_text(tomli_w.dumps(configuration), encoding="utf-8")

        with (
            patch.object(science, "subtract_baseline", wraps=science.subtract_baseline) as baseline,
            patch.object(science, "detect_peaks", wraps=science.detect_peaks) as peaks,
        ):
            async with provider_setup.open_provider() as provider:
                original_open = provider.peaks.host.open_analysis
                attempts = []

                def admit(**options):
                    attempts.append(options)

                    if not (len(attempts) > 1):
                        raise CapacityUnavailable(f"Peak stage is occupied on attempt {len(attempts)}")

                    return original_open(**options)

                with patch.object(provider.peaks.host, "open_analysis", side_effect=admit):
                    report = await provider.summarize(spectrum=self.spectrum)

                self.assertEqual(2, len(report.peaks))
                self.assertEqual(2, len(attempts))
                self.assertEqual(1, baseline.call_count)
                self.assertEqual(1, peaks.call_count)
                self.assertEqual((0, 3, 1, 4, 0), peaks.call_args.kwargs["spectrum"].intensities)

    async def test_lost_peak_submission_reply_is_settled_without_resubmission(self):
        failure = OSError("Peak submission reply was lost after acceptance")
        submissions = []

        async with provider_setup.open_provider() as provider:
            original_open = provider.peaks.host.open_analysis

            def open_analysis(**options):
                analysis = original_open(**options)
                submit = analysis.submit

                def lose_reply(invocation):
                    submissions.append(submit(invocation))

                    raise failure

                analysis.submit = lose_reply

                return analysis

            with patch.object(provider.peaks.host, "open_analysis", side_effect=open_analysis):
                with self.assertRaises(OSError) as raised:
                    async with open_invocation(partial(provider.summarize, spectrum=self.spectrum)) as invocation:
                        for _ in range(2):
                            with self.assertRaises(OSError) as observed:
                                await invocation.wait()

                            self.assertIs(failure, observed.exception)

                self.assertIs(failure, raised.exception)
                self.assertEqual(1, len(submissions))
                self.assertEqual(0, provider.peaks.host.result_reserved)

    async def test_baseline_cleanup_failure_prevents_peak_dispatch_and_replay(self):
        failure = OSError("Baseline analysis cleanup failed")

        async def fail_cleanup(control):
            await settle_cleanup(control)

            raise failure

        with (
            patch.object(science, "subtract_baseline", wraps=science.subtract_baseline) as baseline,
            patch.object(science, "detect_peaks", wraps=science.detect_peaks) as peaks,
        ):
            async with provider_setup.open_provider() as provider:
                with patch("provider_core.scientific_call.settle_cleanup", side_effect=fail_cleanup):
                    with self.assertRaises(OSError) as raised:
                        async with open_invocation(partial(provider.summarize, spectrum=self.spectrum)) as invocation:
                            for _ in range(2):
                                with self.assertRaises(OSError) as observed:
                                    await invocation.wait()

                                self.assertIs(failure, observed.exception)

                    self.assertIs(failure, raised.exception)
                    self.assertEqual(1, baseline.call_count)
                    self.assertEqual(0, peaks.call_count)

    async def test_failed_encoding_preserves_cause_and_never_dispatches_peaks(self):
        codec = JsonDataclassCodec(value_type=science.Spectrum, format="test.spectrum.v1")
        failure = OSError("Cannot encode the calculated spectrum")

        class BrokenCodec:
            value_type = codec.value_type
            format = codec.format

            def encode(self, value):
                raise failure

            def decode(self, *, payload):
                return codec.decode(payload=payload)

        class BrokenBaseline(BaselineCorrector):
            @operation(result_codec=BrokenCodec())
            def run(self, *, spectrum: science.Spectrum) -> science.Spectrum:
                return super().run(spectrum=spectrum)

        with (
            patch.object(science, "subtract_baseline", wraps=science.subtract_baseline) as baseline,
            patch.object(science, "detect_peaks", wraps=science.detect_peaks) as peaks,
            patch("spectrum_provider.provider.BaselineCorrector", BrokenBaseline),
            patch("spectrum_provider.process_factory.BaselineCorrector", BrokenBaseline),
        ):
            async with provider_setup.open_provider() as provider:
                with self.assertRaises(OSError) as raised:
                    async with open_invocation(partial(provider.summarize, spectrum=self.spectrum)) as invocation:
                        for _ in range(2):
                            with self.assertRaises(OSError) as observed:
                                await invocation.wait()

                            self.assertIs(failure, observed.exception)

                self.assertIs(failure, raised.exception)
                self.assertEqual(1, baseline.call_count)
                self.assertEqual(0, peaks.call_count)


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
