"""Prove independent installed stages and retained delivery on both backends."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import os
import pathlib
import tomllib
import unittest
from unittest.mock import patch
from tests.fakes.deployment import configure_backend, staged_configuration

import tomli_w

from provider_core.submission import Submission
from provider_core.hosting import CapacityUnavailable, NotAdmitted, Outcome, ResultConsumption, ResultState, observe_cleanup, settle_cleanup
from tests.fakes.runner import make_scientific_session
from spectrum_provider import provider as provider_setup
from spectrum_provider.science import Spectrum


class SpectrumProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.configuration = self.enterContext(staged_configuration(
            provider=provider_setup,
            source=pathlib.Path(__file__).parent.parent / "config/deployments/localhost/provider.toml",
        ))

    def configure(self, backend):
        return configure_backend(self.configuration, backend)

    def sessions(self):
        sessions = {}

        for stage in ("baseline", "peaks"):
            _, sessions[stage] = make_scientific_session(self)

        return sessions

    async def test_invalid_session_inventory_refuses_before_taking_either_stage(self):
        self.configure("process")
        sessions = self.sessions()
        descriptors = {stage: session.control.fileno() for stage, session in sessions.items()}
        for supplied in (
            {"baseline": sessions["baseline"]},
            {**sessions, "extra": sessions["baseline"]},
            {"baseline": sessions["baseline"], "peaks": sessions["baseline"]},
        ):
            with self.subTest(keys=tuple(supplied)):
                with self.assertRaises(ValueError):
                    async with provider_setup.open_provider(process_sessions=supplied):
                        self.fail("Invalid stage inventories must not yield a provider")
                self.assertEqual(descriptors, {stage: session.control.fileno() for stage, session in sessions.items()})
                self.assertTrue(all(not session.control_connected for session in sessions.values()))
                self.assertTrue(all(session.retirement is None for session in sessions.values()))

    async def test_same_workflow_reuses_independent_stages_and_retains_delivery_on_both_backends(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                self.configure(backend)
                sessions = self.sessions() if backend == "process" else None

                async with provider_setup.open_provider(process_sessions=sessions) as provider:
                    self.assertIsNot(provider.baseline_host, provider.peak_host)
                    if backend == "process":
                        pids = (provider.baseline_host.process_id, provider.peak_host.process_id)
                        self.assertEqual(2, len(set(pids)))
                        self.assertNotIn(os.getpid(), pids)
                        self.assertIsNone(provider.baseline_corrector.contribution)
                        self.assertIsNone(provider.peak_detector.contribution)

                    for _ in range(2):
                        workflow = provider.create_workflow(spectrum=Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)))
                        self.assertNotIsInstance(workflow.start(), NotAdmitted)
                        async with asyncio.timeout(10):
                            observation = await workflow.finish()
                            self.assertIs(Outcome.SUCCEEDED, observation.outcome)
                            original = workflow.peak_detection_submission.operation
                            self.assertIs(original, workflow.peak_detection_submission.submit())
                            await observe_cleanup(workflow.baseline_report_release)
                            await observe_cleanup(workflow.baseline_analysis_executor.close())
                            await observe_cleanup(workflow.peak_analysis_executor.close())
                            payload = await original.read_result(observation.result)
                            self.assertEqual([3.0, 4.0], [peak.height for peak in provider.peak_call.result_codec.decode(payload=payload).peaks])

                            failure = OSError("The receiver refused the report")

                            async def refuse(payload):
                                raise failure

                            consumption = ResultConsumption(operation=original, result=observation.result, receive=refuse)
                            with self.assertRaises(OSError) as raised:
                                await consumption.consume()
                            self.assertIs(failure, raised.exception)
                            self.assertEqual(payload, await original.read_result(observation.result))
                            received = []

                            async def accept(payload):
                                received.append(payload)

                            accepted = ResultConsumption(operation=original, result=observation.result, receive=accept)
                            await accepted.consume()
                            await accepted.consume()
                            self.assertEqual([payload], received)
                            released = await original.observe(after=None, wait_seconds=0)
                            self.assertIs(ResultState.RELEASED, released.result_state)

                        if backend == "process":
                            self.assertEqual(pids, (provider.baseline_host.process_id, provider.peak_host.process_id))

    async def test_retiring_one_stage_leaves_the_other_available_on_both_backends(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                self.configure(backend)
                sessions = self.sessions() if backend == "process" else None
                async with provider_setup.open_provider(process_sessions=sessions) as provider:
                    await settle_cleanup(provider.baseline_host.retire())
                    analysis = provider.peak_host.open_analysis(allocations=frozenset({provider.peak_detector.allocation}))
                    peak = Submission.from_call(
                        analysis_executor=analysis,
                        call_binding=provider.peak_call,
                        scientific_input=Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0)),
                        states={},
                        reservation_ref=analysis.reservation(allocation=provider.peak_detector.allocation),
                        operation_policy=provider.operation_policy,
                    )
                    self.assertNotIsInstance(peak.submit(), NotAdmitted)
                    async with asyncio.timeout(10):
                        observation = await peak.wait()
                        self.assertIs(Outcome.SUCCEEDED, observation.outcome)
                        report = provider.peak_call.result_codec.decode(payload=await peak.operation.read_result(observation.result))
                        self.assertEqual([3.0], [point.height for point in report.peaks])
                        await settle_cleanup(peak.operation.release_result(observation.result))
                        await settle_cleanup(analysis.close())
                    if backend == "process":
                        os.kill(provider.peak_host.process_id, 0)

    async def test_overallocated_partition_is_rejected_before_sessions_are_taken(self):
        configuration = self.configure("process")
        configuration["execution"]["stage_capacity"]["peaks"]["result_bytes"] = 8192
        self.configuration.write_text(tomli_w.dumps(configuration), encoding="utf-8")
        sessions = self.sessions()
        descriptors = {stage: session.control.fileno() for stage, session in sessions.items()}
        with self.assertRaisesRegex(ValueError, "result_bytes.*assigned.*allowance"):
            async with provider_setup.open_provider(process_sessions=sessions):
                self.fail("Overallocated stage shares were accepted")
        self.assertFalse(sessions["baseline"]._connect_started)
        self.assertFalse(sessions["peaks"]._connect_started)
        self.assertEqual(descriptors, {stage: session.control.fileno() for stage, session in sessions.items()})

    async def test_peak_reservation_refusal_closes_original_baseline_analysis(self):
        self.configure("thread")
        async with provider_setup.open_provider() as provider:
            cause = CapacityUnavailable("The peak stage is occupied")
            original_open = provider.baseline_host.open_analysis
            cleanup_controls = []

            def remember(**options):
                analysis = original_open(**options)
                original_close = analysis.close

                def retain_close():
                    control = original_close()
                    cleanup_controls.append(control)
                    return control

                analysis.close = retain_close
                return analysis

            with patch.object(provider.baseline_host, "open_analysis", side_effect=remember), patch.object(provider.peak_host, "open_analysis", side_effect=cause):
                with self.assertRaises(CapacityUnavailable) as raised:
                    provider.create_workflow(spectrum=Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0)))
                self.assertIs(cause, raised.exception)

            self.assertEqual(1, len(cleanup_controls))
            await settle_cleanup(cleanup_controls[0])
            workflow = provider.create_workflow(spectrum=Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0)))
            self.assertNotIsInstance(workflow.start(), NotAdmitted)
            async with asyncio.timeout(10):
                self.assertIs(Outcome.SUCCEEDED, (await workflow.finish()).outcome)
            await settle_cleanup(workflow.baseline_analysis_executor.close())
            await settle_cleanup(workflow.peak_analysis_executor.close())

    async def test_baseline_scientific_failure_does_not_execute_or_retire_peak_stage(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                configuration = self.configure(backend)
                configuration["science"]["baseline"]["settings"]["baseline"] = -1e308
                self.configuration.write_text(tomli_w.dumps(configuration), encoding="utf-8")
                sessions = self.sessions() if backend == "process" else None
                async with provider_setup.open_provider(process_sessions=sessions) as provider:
                    workflow = provider.create_workflow(spectrum=Spectrum(positions=(0, 1, 2), intensities=(1e308, 1e308, 1e308)))
                    original = workflow.start()
                    async with asyncio.timeout(10):
                        failed = await workflow.finish()
                        self.assertIs(Outcome.FAILED, failed.outcome)
                        self.assertTrue(failed.failures)
                        self.assertIsNone(workflow.peak_detection_submission)
                        self.assertIs(original, workflow.start())
                        self.assertIs(Outcome.FAILED, (await workflow.finish()).outcome)
                        await settle_cleanup(workflow.baseline_analysis_executor.close())
                        await settle_cleanup(workflow.peak_analysis_executor.close())
                    peak_analysis = provider.peak_host.open_analysis(allocations=frozenset({provider.peak_detector.allocation}))
                    await settle_cleanup(peak_analysis.close())
