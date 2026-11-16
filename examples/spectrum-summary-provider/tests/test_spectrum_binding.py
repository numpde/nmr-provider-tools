"""Check configured registrations and portable stage boundaries after installation."""

from provider_core.container_guard import require_container

require_container()

from importlib import metadata, resources
from pathlib import Path
import sys
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import provider_core.contribution as contribution
import provider_core.scientific as sdk
from spectrum_provider import science
from spectrum_provider.binding import BaselineCorrector, PeakDetector
from spectrum_provider import provider as provider_setup


class SpectrumBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.configuration = directory / "provider.toml"

        shutil.copyfile(
            src=Path(__file__).parent.parent / "config/deployments/localhost/provider.toml",
            dst=self.configuration,
        )

        self.enterContext(patch.object(target=provider_setup, attribute="PROVIDER_CONFIG_PATH", new=self.configuration))

    async def provider(self):
        return await self.enterAsyncContext(provider_setup.open_provider())

    async def test_installed_distribution_contains_its_package(self) -> None:
        distribution = metadata.distribution("spectrum-summary-provider-example")
        package = Path(resources.files("spectrum_provider")).resolve()

        self.assertEqual(package, Path(distribution.locate_file("spectrum_provider")).resolve())
        self.assertTrue(package.is_relative_to(Path(sys.prefix).resolve()))

        for name in ("science.py", "binding.py", "provider.py"):
            with self.subTest(name=name):
                self.assertTrue(package.joinpath(name).is_file())
                self.assertIn(f"spectrum_provider/{name}", {str(path) for path in distribution.files})

    async def test_independent_bindings_share_the_typed_spectrum_format(self) -> None:
        provider = await self.provider()
        baseline_corrector = provider.baseline_corrector
        peak_detector = provider.peak_detector
        baseline_call_binding = baseline_corrector.call(method=BaselineCorrector.run)
        peak_detection_call_binding = peak_detector.call(method=PeakDetector.run)

        self.assertNotEqual(baseline_corrector.description.binding_id, peak_detector.description.binding_id)
        self.assertRegex(baseline_corrector.description.binding_revision, r"\Aconfigured-v3:sha256:[0-9a-f]{64}\Z")
        self.assertRegex(peak_detector.description.binding_revision, r"\Aconfigured-v3:sha256:[0-9a-f]{64}\Z")
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

        for binding in (provider.baseline_corrector, provider.peak_detector):
            with self.subTest(binding=binding.description.binding_id):
                self.assertIs(type(binding.contribution).initialize, sdk.Contribution.initialize)
                self.assertIsNone(binding.contribution.analysis_type)
                self.assertEqual(frozenset(), binding.description.resident_resources)
                call = binding.call(method=binding.contribution.run)

                self.assertEqual(frozenset({expected}), call.description.resources)
                self.assertEqual((), call.description.state_inputs)
                self.assertEqual((), call.description.state_outputs)

    async def test_provider_settings_remain_local_to_each_implementation(self) -> None:
        original = await self.provider()
        configured = self.configuration.read_text(encoding="utf-8").replace("baseline = 1.0", "baseline = 2.0")

        self.configuration.write_text(data=configured, encoding="utf-8")
        changed = await self.provider()

        self.configuration.write_text(data="# Formatting does not change settings\n" + configured, encoding="utf-8")
        reformatted = await self.provider()

        self.configuration.write_text(data=configured.replace("minimum_height = 2.0", "minimum_height = 4.0"), encoding="utf-8")
        changed_peak_settings = await self.provider()

        self.assertNotEqual(original.baseline_corrector.contribution.configuration, changed.baseline_corrector.contribution.configuration)
        self.assertEqual(original.peak_detector.description.binding_revision, changed.peak_detector.description.binding_revision)
        self.assertEqual(changed.baseline_corrector.description.binding_revision, reformatted.baseline_corrector.description.binding_revision)
        self.assertEqual(changed.peak_detector.description.binding_revision, reformatted.peak_detector.description.binding_revision)
        self.assertEqual(changed.baseline_corrector.description.binding_revision, changed_peak_settings.baseline_corrector.description.binding_revision)
        self.assertNotEqual(changed.peak_detector.contribution.configuration, changed_peak_settings.peak_detector.contribution.configuration)

        sample = science.Spectrum(positions=(0, 1, 2), intensities=(1, 4, 1))
        original_instance = original.baseline_corrector.contribution
        changed_instance = changed.baseline_corrector.contribution

        self.assertEqual((0, 3, 0), original_instance.run(spectrum=sample).intensities)
        self.assertEqual((-1, 2, -1), changed_instance.run(spectrum=sample).intensities)


if __name__ == "__main__":
    unittest.main()
