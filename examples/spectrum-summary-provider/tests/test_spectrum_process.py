"""Prove independent installed stages and retained delivery on both backends."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from functools import partial
import os
import pathlib
import tomllib
import unittest
from unittest.mock import patch
from tests.fakes.deployment import configure_backend, staged_configuration

import tomli_w

from provider_core.hosting import CapacityUnavailable, settle_cleanup
from provider_core.scientific_invocation import open_invocation
from provider_core.process_wire import RemoteScienceError
from tests.fakes.runner import make_scientific_session
from spectrum_provider import provider as provider_setup
from spectrum_provider.science import Spectrum


class SpectrumProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.configuration = self.enterContext(
            staged_configuration(
                provider=provider_setup,
                source=pathlib.Path(__file__).parent.parent / "config/deployments/localhost/provider.toml",
            ),
        )

    def configure(self, backend):
        return configure_backend(self.configuration, backend)

    def sessions(self):
        sessions = {}

        for stage in ("baseline", "peaks"):
            (_, sessions[stage]) = make_scientific_session(self)

        return sessions

    async def test_invalid_session_inventory_refuses_before_taking_either_stage(self):
        self.configure("process")
        sessions = self.sessions()
        descriptors = {stage: session.control.fileno() for (stage, session) in sessions.items()}

        for supplied in (
            {"baseline": sessions["baseline"]},
            {**sessions, "extra": sessions["baseline"]},
            {"baseline": sessions["baseline"], "peaks": sessions["baseline"]},
        ):
            with self.subTest(keys=tuple(supplied)):
                with self.assertRaises(ValueError):
                    async with provider_setup.open_provider(process_sessions=supplied):
                        self.fail("Invalid stage inventories must not yield a provider")

                self.assertEqual(descriptors, {stage: session.control.fileno() for (stage, session) in sessions.items()})
                self.assertTrue(all((not session.control_connected) for session in sessions.values()))
                self.assertTrue(all((session.retirement is None) for session in sessions.values()))

    async def test_same_sequence_reuses_independent_stages_on_both_backends(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                self.configure(backend)

                # silent-defaults: allow-contract -- The thread backend has no runner sessions
                sessions = self.sessions() if backend == "process" else None

                async with provider_setup.open_provider(process_sessions=sessions) as provider:
                    self.assertIsNot(provider.baseline.host, provider.peaks.host)

                    if backend == "process":
                        pids = (provider.baseline.host.process_id, provider.peaks.host.process_id)

                        self.assertEqual(2, len(set(pids)))
                        self.assertNotIn(os.getpid(), pids)

                    for _ in range(2):
                        async with open_invocation(
                            partial(
                                provider.summarize,
                                spectrum=Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)),
                            ),
                        ) as invocation:
                            async with asyncio.timeout(10):
                                report = await invocation.wait()

                                self.assertIs(report, await invocation.wait())

                            self.assertEqual([3.0, 4.0], [peak.height for peak in report.peaks])

                        if backend == "process":
                            self.assertEqual(pids, (provider.baseline.host.process_id, provider.peaks.host.process_id))

    async def test_retiring_one_stage_leaves_the_other_available_on_both_backends(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                self.configure(backend)

                # silent-defaults: allow-contract -- The thread backend has no runner sessions
                sessions = self.sessions() if backend == "process" else None

                async with provider_setup.open_provider(process_sessions=sessions) as provider:
                    await settle_cleanup(provider.baseline.host.retire())

                    async with asyncio.timeout(10):
                        report = await provider.peaks(scientific_input=Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0)))

                    self.assertEqual([3.0], [point.height for point in report.peaks])

                    if backend == "process":
                        os.kill(provider.peaks.host.process_id, 0)

    async def test_overallocated_partition_is_rejected_before_sessions_are_taken(self):
        configuration = self.configure("process")
        configuration["execution"]["stage_capacity"]["peaks"]["result_bytes"] = 8192

        self.configuration.write_text(tomli_w.dumps(configuration), encoding="utf-8")
        sessions = self.sessions()
        descriptors = {stage: session.control.fileno() for (stage, session) in sessions.items()}

        with self.assertRaisesRegex(ValueError, "result_bytes.*assigned.*allowance"):
            async with provider_setup.open_provider(process_sessions=sessions):
                self.fail("Overallocated stage shares were accepted")

        self.assertFalse(sessions["baseline"]._connect_started)
        self.assertFalse(sessions["peaks"]._connect_started)
        self.assertEqual(descriptors, {stage: session.control.fileno() for (stage, session) in sessions.items()})

    async def test_peak_capacity_failure_settles_baseline_without_replay(self):
        self.configure("thread")

        async with provider_setup.open_provider() as provider:
            failure = CapacityUnavailable("The peak stage is occupied")

            with patch.object(provider.peaks.host, "open_analysis", side_effect=failure):
                with self.assertRaises(CapacityUnavailable) as raised:
                    await provider.summarize(spectrum=Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0)))

            self.assertIs(failure, raised.exception)
            self.assertEqual(0, provider.baseline.host.result_reserved)
            report = await provider.summarize(spectrum=Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0)))

            self.assertEqual([2.0], [point.height for point in report.peaks])

    async def test_baseline_scientific_failure_does_not_execute_or_retire_peak_stage(self):
        for backend in ("thread", "process"):
            with self.subTest(backend=backend):
                configuration = self.configure(backend)
                configuration["science"]["baseline"]["settings"]["baseline"] = -1e308

                self.configuration.write_text(tomli_w.dumps(configuration), encoding="utf-8")

                # silent-defaults: allow-contract -- The thread backend has no runner sessions
                sessions = self.sessions() if backend == "process" else None

                async with provider_setup.open_provider(process_sessions=sessions) as provider:
                    with patch.object(provider.peaks.host, "open_analysis", wraps=provider.peaks.host.open_analysis) as peak_analysis:
                        with self.assertRaisesRegex((ArithmeticError, RemoteScienceError), "corrected_intensity"):
                            async with open_invocation(
                                partial(
                                    provider.summarize,
                                    spectrum=Spectrum(positions=(0, 1, 2), intensities=(1e308, 1e308, 1e308)),
                                ),
                            ) as invocation:
                                await invocation.wait()

                        peak_analysis.assert_not_called()

                    report = await provider.peaks(scientific_input=Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0)))

                    self.assertEqual([3.0], [point.height for point in report.peaks])
