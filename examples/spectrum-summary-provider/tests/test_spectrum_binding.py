"""Check configured registrations and portable stage boundaries after installation."""

from provider_core.container_guard import require_container

require_container()

from importlib import metadata, resources
import pathlib
import sys
import unittest
from tests.fakes.deployment import staged_configuration

import provider_core.contribution as contribution
import provider_core.scientific as sdk
from spectrum_provider import science
from spectrum_provider.binding import BaselineCorrector, PeakDetector
from spectrum_provider import provider as provider_setup


class SpectrumBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.configuration = self.enterContext(
            staged_configuration(
                provider=provider_setup,
                source=pathlib.Path(__file__).parent.parent / "config/deployments/localhost/provider.toml",
            ),
        )

    async def provider(self):
        return await self.enterAsyncContext(provider_setup.open_provider())

    async def test_installed_distribution_contains_its_package(self) -> None:
        distribution = metadata.distribution("spectrum-summary-provider-example")
        package = pathlib.Path(resources.files("spectrum_provider")).resolve()

        self.assertEqual(package, pathlib.Path(distribution.locate_file("spectrum_provider")).resolve())
        self.assertTrue(package.is_relative_to(pathlib.Path(sys.prefix).resolve()))

        for name in ("science.py", "binding.py", "provider.py"):
            with self.subTest(name=name):
                self.assertTrue(package.joinpath(name).is_file())
                self.assertIn(f"spectrum_provider/{name}", {str(distribution_file) for distribution_file in distribution.files})

    async def test_independent_bindings_share_the_typed_spectrum_format(self) -> None:
        provider = await self.provider()
        baseline_call_binding = provider.baseline.binding
        peak_detection_call_binding = provider.peaks.binding

        self.assertNotEqual(contribution.ref(baseline_call_binding).binding_id, contribution.ref(peak_detection_call_binding).binding_id)
        self.assertRegex(contribution.ref(baseline_call_binding).binding_revision, r"\Aconfigured-v4:sha256:[0-9a-f]{64}\Z")
        self.assertRegex(contribution.ref(peak_detection_call_binding).binding_revision, r"\Aconfigured-v4:sha256:[0-9a-f]{64}\Z")
        self.assertEqual("run", baseline_call_binding.description.name)
        self.assertEqual("run", peak_detection_call_binding.description.name)
        self.assertIs(science.Spectrum, baseline_call_binding.input_codec.value_type)
        self.assertIs(science.Spectrum, baseline_call_binding.result_codec.value_type)
        self.assertIs(science.Spectrum, peak_detection_call_binding.input_codec.value_type)
        self.assertIs(science.PeakReport, peak_detection_call_binding.result_codec.value_type)
        self.assertEqual(baseline_call_binding.result_codec.format, peak_detection_call_binding.input_codec.format)

        corrected = science.Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0))

        self.assertEqual(
            corrected,
            peak_detection_call_binding.decode_input(baseline_call_binding.encode_result(corrected)),
        )

    async def test_bindings_declare_only_call_resources_and_no_scientific_lifetimes(self) -> None:
        expected = contribution.ResourceRequirement(
            kind="cpu",
            amount=1,
            unit="thread",
            selector="generic",
            exclusive=False,
        )

        provider = await self.provider()

        for scientific_call in (provider.baseline, provider.peaks):
            call = scientific_call.binding

            with self.subTest(binding=contribution.ref(call).binding_id):
                self.assertEqual(frozenset({expected}), call.description.resources)
                self.assertEqual((), call.description.state_inputs)
                self.assertEqual((), call.description.state_outputs)

        for contribution_type in (BaselineCorrector, PeakDetector):
            self.assertIs(contribution_type.initialize, sdk.Contribution.initialize)
            self.assertIsNone(contribution_type.analysis_type)

    async def test_provider_settings_remain_local_to_each_implementation(self) -> None:
        original = await self.provider()
        configured = self.configuration.read_text(encoding="utf-8").replace("baseline = 1.0", "baseline = 2.0")

        self.configuration.write_text(data=configured, encoding="utf-8")
        changed = await self.provider()

        self.configuration.write_text(data="# Formatting does not change settings\n" + configured, encoding="utf-8")
        reformatted = await self.provider()

        self.configuration.write_text(data=configured.replace("minimum_height = 2.0", "minimum_height = 4.0"), encoding="utf-8")
        changed_peak_settings = await self.provider()

        self.assertEqual(contribution.ref(original.peaks.binding).binding_revision, contribution.ref(changed.peaks.binding).binding_revision)
        self.assertEqual(contribution.ref(changed.baseline.binding).binding_revision, contribution.ref(reformatted.baseline.binding).binding_revision)
        self.assertEqual(contribution.ref(changed.peaks.binding).binding_revision, contribution.ref(reformatted.peaks.binding).binding_revision)
        self.assertEqual(contribution.ref(changed.baseline.binding).binding_revision, contribution.ref(changed_peak_settings.baseline.binding).binding_revision)

        sample = science.Spectrum(positions=(0, 1, 2), intensities=(1, 4, 1))
        original_corrected = await original.baseline(scientific_input=sample)
        changed_corrected = await changed.baseline(scientific_input=sample)
        original_peaks = await original.peaks(scientific_input=original_corrected)
        changed_peaks = await changed_peak_settings.peaks(scientific_input=original_corrected)

        self.assertEqual((0, 3, 0), original_corrected.intensities)
        self.assertEqual((-1, 2, -1), changed_corrected.intensities)
        self.assertEqual(1, len(original_peaks.peaks))
        self.assertEqual((), changed_peaks.peaks)


if __name__ == "__main__":
    unittest.main()
