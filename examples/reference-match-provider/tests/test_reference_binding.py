"""Check installed configuration, typed calls and reusable index ownership."""

from provider_core.container_guard import require_container

require_container()

from importlib import metadata, resources
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from provider_core.embedded_host import EmbeddedHost, HostLimits, ResourceAllocation
from provider_core.hosting import ResourceGrant, observe_cleanup
from reference_provider import science
from reference_provider.binding import ReferenceMatcher, create_binding


class ReferenceBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = EmbeddedHost(limits=HostLimits(
              input_bytes=4096, result_bytes=4096,
            state_values=1, progress_records=1, initialization_seconds=5, retirement_seconds=5,
        ))
        self.addAsyncCleanup(self.retire)

    async def retire(self):
        await observe_cleanup(control=self.host.retire())

    def binding(self):
        return create_binding(host=self.host, allocation=ResourceAllocation(
            name=f"library-{len(self.host.allocations)}",
            grants=(ResourceGrant(kind="cpu", amount=1, unit="thread", selector="generic"),),
        ))

    async def test_installed_distribution_contains_the_scientific_package_and_settings(self):
        distribution = metadata.distribution("reference-match-provider-example")
        package = Path(resources.files("reference_provider")).resolve()
        self.assertEqual(package, Path(distribution.locate_file("reference_provider")).resolve())
        self.assertTrue(package.is_relative_to(Path(sys.prefix).resolve()))
        for name in ("science.py", "binding.py", "settings.toml", "resources.toml"):
            self.assertTrue(package.joinpath(name).is_file())
            self.assertIn(f"reference_provider/{name}", {str(path) for path in distribution.files})

    async def test_one_typed_query_call_has_no_analysis_or_state_lifetime(self):
        binding = self.binding()
        call = binding.call(method=ReferenceMatcher.match)
        self.assertRegex(binding.description.binding_revision, r"\Aconfigured-v3:sha256:[0-9a-f]{64}\Z")
        self.assertEqual("match", call.description.name)
        self.assertIs(science.VectorInput, call.input_codec.value_type)
        self.assertIs(science.MatchReport, call.result_codec.value_type)
        self.assertIsNone(binding.contribution.analysis_type)
        self.assertEqual((), call.description.state_inputs)
        self.assertEqual((), call.description.state_outputs)
        self.assertEqual(1, call.description.resources[0].amount)

    async def test_factory_defers_index_building_to_the_instance_initializer(self):
        with patch("reference_provider.science.build_index", wraps=science.build_index) as build:
            binding = self.binding()
            library = binding.contribution
            build.assert_not_called()
            self.assertIsNone(library.loaded_index)

    async def test_provider_settings_change_without_becoming_sdk_configuration(self):
        original = self.binding()
        package = resources.files("reference_provider")
        source = package.joinpath("settings.toml").read_text(encoding="utf-8")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("resources.toml").write_text(package.joinpath("resources.toml").read_text(encoding="utf-8"), encoding="utf-8")
            with patch("reference_provider.binding.files", return_value=root):
                root.joinpath("settings.toml").write_text(source.replace("minimum_score = 0.5", "minimum_score = 0.9"), encoding="utf-8")
                changed_settings = self.binding()
                root.joinpath("settings.toml").write_text(source.replace("3.0, 4.0, 0.0, 0.0", "6.0, 4.0, 0.0, 0.0"), encoding="utf-8")
                changed_references = self.binding()
                root.joinpath("settings.toml").write_text("# Same values\n" + source, encoding="utf-8")
                reformatted = self.binding()
        self.assertEqual(original.description.binding_revision, changed_settings.description.binding_revision)
        self.assertEqual(original.description.binding_revision, changed_references.description.binding_revision)
        self.assertEqual(original.description.binding_revision, reformatted.description.binding_revision)
        self.assertEqual(original.description.assets, changed_settings.description.assets)
        self.assertNotEqual(original.contribution.configuration, changed_references.contribution.configuration)
        self.assertEqual(0.5, original.contribution.configuration.settings.minimum_score)
        self.assertEqual((3, 4, 0, 0), original.contribution.configuration.references.entries[0].values)


if __name__ == "__main__":
    unittest.main()
