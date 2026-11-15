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
from spectrum_provider.binding import Baseline, Peaks
from spectrum_provider import provider as provider_setup


class SpectrumBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.configuration = directory / "provider.toml"
        shutil.copyfile(
            src=Path(__file__).parent / "config/deployments/localhost/provider.toml",
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
        baseline = provider.baseline
        peaks = provider.peaks
        baseline_call = baseline.call(method=Baseline.run)
        peak_call = peaks.call(method=Peaks.run)
        self.assertNotEqual(baseline.description.binding_id, peaks.description.binding_id)
        self.assertRegex(baseline.description.binding_revision, r"\Aconfigured-v3:sha256:[0-9a-f]{64}\Z")
        self.assertRegex(peaks.description.binding_revision, r"\Aconfigured-v3:sha256:[0-9a-f]{64}\Z")
        self.assertEqual("run", baseline_call.description.name)
        self.assertEqual("run", peak_call.description.name)
        self.assertIs(science.Spectrum, baseline_call.input_codec.value_type)
        self.assertIs(science.Spectrum, baseline_call.result_codec.value_type)
        self.assertIs(science.Spectrum, peak_call.input_codec.value_type)
        self.assertIs(science.PeakReport, peak_call.result_codec.value_type)
        self.assertEqual(baseline_call.result_codec.format, peak_call.input_codec.format)

        corrected = science.Spectrum(positions=(0, 1, 2), intensities=(0, 3, 0))
        self.assertEqual(
            corrected,
            peak_call.decode_input(baseline_call.encode_result(corrected)),
        )

    async def test_bindings_declare_only_call_resources_and_no_scientific_lifetimes(self) -> None:
        expected = contribution.ResourceRequirement(
            kind="cpu", amount=1, unit="thread", selector="generic", exclusive=False,
        )
        provider = await self.provider()
        for binding in (provider.baseline, provider.peaks):
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

        self.assertNotEqual(original.baseline.contribution.configuration, changed.baseline.contribution.configuration)
        self.assertEqual(original.peaks.description.binding_revision, changed.peaks.description.binding_revision)
        self.assertEqual(changed.baseline.description.binding_revision, reformatted.baseline.description.binding_revision)
        self.assertEqual(changed.peaks.description.binding_revision, reformatted.peaks.description.binding_revision)
        self.assertEqual(changed.baseline.description.binding_revision, changed_peak_settings.baseline.description.binding_revision)
        self.assertNotEqual(changed.peaks.contribution.configuration, changed_peak_settings.peaks.contribution.configuration)

        sample = science.Spectrum(positions=(0, 1, 2), intensities=(1, 4, 1))
        original_instance = original.baseline.contribution
        changed_instance = changed.baseline.contribution
        self.assertEqual((0, 3, 0), original_instance.run(spectrum=sample).intensities)
        self.assertEqual((-1, 2, -1), changed_instance.run(spectrum=sample).intensities)


if __name__ == "__main__":
    unittest.main()
