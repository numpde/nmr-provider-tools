"""Check installed configuration, typed calls and reusable index ownership."""

from provider_core.container_guard import require_container

require_container()

from importlib import metadata, resources
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import provider_core.scientific as sdk
from reference_provider import science
from reference_provider.binding import ReferenceMatcher, create_binding


class ReferenceBindingTests(unittest.TestCase):
    def test_installed_distribution_contains_the_scientific_package_and_settings(self):
        distribution = metadata.distribution("reference-match-provider-example")
        package = Path(resources.files("reference_provider")).resolve()
        self.assertEqual(package, Path(distribution.locate_file("reference_provider")).resolve())
        self.assertTrue(package.is_relative_to(Path(sys.prefix).resolve()))
        for name in ("science.py", "binding.py", "settings.toml", "resources.toml"):
            self.assertTrue(package.joinpath(name).is_file())
            self.assertIn(f"reference_provider/{name}", {str(path) for path in distribution.files})

    def test_one_typed_query_call_has_no_analysis_or_state_lifetime(self):
        binding = create_binding()
        call = binding.call(method=ReferenceMatcher.match)
        self.assertRegex(binding.description.binding_revision, r"\Aconfigured-v3:sha256:[0-9a-f]{64}\Z")
        self.assertEqual("match", call.description.name)
        self.assertIs(science.VectorInput, call.input_codec.value_type)
        self.assertIs(science.MatchReport, call.result_codec.value_type)
        self.assertIsNone(binding.contribution.analysis_type)
        self.assertEqual((), call.description.state_inputs)
        self.assertEqual((), call.description.state_outputs)
        self.assertEqual(1, call.description.resources[0].amount)

    def test_factory_defers_index_building_to_the_instance_initializer(self):
        with patch("reference_provider.science.build_index", wraps=science.build_index) as build:
            binding = create_binding()
            library = binding.construct_instance()
            build.assert_not_called()
            self.assertIsNone(library.loaded_index)
            binding.initialize_instance(instance=library, assets=sdk.NoAssets())
            build.assert_called_once_with(reference_set=library.configuration.references)
            index = library.loaded_index
            returned = library.match(candidate=science.VectorInput(values=(3, 4, 0, 0)))
            self.assertEqual("pattern-a", returned.label)
            self.assertAlmostEqual(1, returned.score)
            self.assertIs(index, library.loaded_index)
            binding.close_instance(instance=library)
            self.assertIsNone(library.loaded_index)
            self.assertEqual("pattern-a", returned.label)

    def test_settings_and_reference_assets_change_revision_without_mutating_existing_bindings(self):
        original = create_binding()
        package = resources.files("reference_provider")
        source = package.joinpath("settings.toml").read_text(encoding="utf-8")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("resources.toml").write_text(package.joinpath("resources.toml").read_text(encoding="utf-8"), encoding="utf-8")
            with patch("reference_provider.binding.files", return_value=root):
                root.joinpath("settings.toml").write_text(source.replace("minimum_score = 0.5", "minimum_score = 0.9"), encoding="utf-8")
                changed_settings = create_binding()
                root.joinpath("settings.toml").write_text(source.replace("3.0, 4.0, 0.0, 0.0", "6.0, 4.0, 0.0, 0.0"), encoding="utf-8")
                changed_references = create_binding()
                root.joinpath("settings.toml").write_text("# Same values\n" + source, encoding="utf-8")
                reformatted = create_binding()
        self.assertNotEqual(original.description.binding_revision, changed_settings.description.binding_revision)
        self.assertNotEqual(original.description.binding_revision, changed_references.description.binding_revision)
        self.assertEqual(original.description.binding_revision, reformatted.description.binding_revision)
        self.assertEqual(original.description.assets, changed_settings.description.assets)
        self.assertNotEqual(original.description.configuration, changed_references.description.configuration)
        self.assertEqual(0.5, original.configuration.settings.minimum_score)
        self.assertEqual((3, 4, 0, 0), original.configuration.references.entries[0].values)


if __name__ == "__main__":
    unittest.main()
