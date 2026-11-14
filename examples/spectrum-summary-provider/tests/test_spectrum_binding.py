"""Check configured registrations and portable stage boundaries after installation."""

from provider_core.container_guard import require_container

require_container()

from importlib import metadata, resources
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import provider_core.contribution as contribution
import provider_core.scientific as sdk
from provider_core.embedded_host import EmbeddedHost, ResourceAllocation
from provider_core.hosting import ResourceGrant, observe_cleanup
from spectrum_provider import science
from spectrum_provider.binding import Baseline, Peaks, create_bindings


class SpectrumBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = EmbeddedHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )
        self.addAsyncCleanup(self.retire)

    async def retire(self):
        await observe_cleanup(control=self.host.retire())

    def bindings(self):
        grants = (ResourceGrant(kind="cpu", amount=1, unit="thread", selector="generic"),)
        suffix = len(self.host.allocations)
        return create_bindings(
            host=self.host,
            baseline_allocation=ResourceAllocation(name=f"baseline-{suffix}", grants=grants),
            peak_allocation=ResourceAllocation(name=f"peaks-{suffix}", grants=grants),
        )

    async def test_installed_distribution_contains_its_package_and_configuration(self) -> None:
        distribution = metadata.distribution("spectrum-summary-provider-example")
        package = Path(resources.files("spectrum_provider")).resolve()
        self.assertEqual(package, Path(distribution.locate_file("spectrum_provider")).resolve())
        self.assertTrue(package.is_relative_to(Path(sys.prefix).resolve()))
        for name in ("science.py", "binding.py", "settings.toml", "resources.toml"):
            with self.subTest(name=name):
                self.assertTrue(package.joinpath(name).is_file())
                self.assertIn(f"spectrum_provider/{name}", {str(path) for path in distribution.files})

    async def test_independent_bindings_share_the_typed_spectrum_format(self) -> None:
        bindings = self.bindings()
        baseline, peaks = bindings.baseline, bindings.peaks
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
        bindings = self.bindings()
        for binding in (bindings.baseline, bindings.peaks):
            with self.subTest(binding=binding.description.binding_id):
                self.assertIs(type(binding.contribution).initialize, sdk.Contribution.initialize)
                self.assertIsNone(binding.contribution.analysis_type)
                self.assertEqual((), binding.description.resident_resources)
                call = binding.call(method=binding.contribution.run)
                self.assertEqual((expected,), call.description.resources)
                self.assertEqual((), call.description.state_inputs)
                self.assertEqual((), call.description.state_outputs)

    async def test_provider_settings_remain_local_to_each_implementation(self) -> None:
        original = self.bindings()
        original_baseline, original_peaks = original.baseline, original.peaks
        package = resources.files("spectrum_provider")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("resources.toml").write_text(
                package.joinpath("resources.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            configured = "[baseline]\nbaseline = 2.0\n[peaks]\nminimum_height = 2.0\n"
            root.joinpath("settings.toml").write_text(configured, encoding="utf-8")
            with patch("spectrum_provider.binding.files", return_value=root):
                changed = self.bindings()
                changed_baseline, same_peaks = changed.baseline, changed.peaks
                root.joinpath("settings.toml").write_text(
                    "# Formatting does not change scientific settings\n" + configured,
                    encoding="utf-8",
                )
                reformatted = self.bindings()
                reformatted_baseline, reformatted_peaks = reformatted.baseline, reformatted.peaks
                root.joinpath("settings.toml").write_text(
                    "[baseline]\nbaseline = 2.0\n[peaks]\nminimum_height = 4.0\n",
                    encoding="utf-8",
                )
                changed_peak_settings = self.bindings()
                same_baseline, changed_peaks = changed_peak_settings.baseline, changed_peak_settings.peaks

        self.assertNotEqual(original_baseline.contribution.configuration, changed_baseline.contribution.configuration)
        self.assertEqual(original_peaks.description.binding_revision, same_peaks.description.binding_revision)
        self.assertEqual(changed_baseline.description.binding_revision, reformatted_baseline.description.binding_revision)
        self.assertEqual(same_peaks.description.binding_revision, reformatted_peaks.description.binding_revision)
        self.assertEqual(changed_baseline.description.binding_revision, same_baseline.description.binding_revision)
        self.assertNotEqual(same_peaks.contribution.configuration, changed_peaks.contribution.configuration)

        sample = science.Spectrum(positions=(0, 1, 2), intensities=(1, 4, 1))
        original_call = original_baseline.call(method=Baseline.run)
        changed_call = changed_baseline.call(method=Baseline.run)
        original_instance = original_baseline.contribution
        changed_instance = changed_baseline.contribution
        self.assertEqual((0, 3, 0), original_instance.run(spectrum=sample).intensities)
        self.assertEqual((-1, 2, -1), changed_instance.run(spectrum=sample).intensities)


if __name__ == "__main__":
    unittest.main()
