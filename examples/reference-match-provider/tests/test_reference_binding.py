"""Check installed configuration, typed calls and reusable index ownership."""

from provider_core.container_guard import require_container

require_container()

from importlib import metadata, resources
from pathlib import Path
import sys
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reference_provider import science
from reference_provider.binding import ReferenceMatcher
from reference_provider import provider as provider_setup


class ReferenceBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.configuration = directory / "provider.toml"

        shutil.copyfile(
            src=Path(__file__).parent / "config/deployments/localhost/provider.toml",
            dst=self.configuration,
        )

        self.enterContext(patch.object(target=provider_setup, attribute="PROVIDER_CONFIG_PATH", new=self.configuration))

    async def binding(self):
        provider = await self.enterAsyncContext(provider_setup.open_provider())

        return provider.matcher

    async def test_installed_distribution_contains_the_scientific_package(self):
        distribution = metadata.distribution("reference-match-provider-example")
        package = Path(resources.files("reference_provider")).resolve()

        self.assertEqual(package, Path(distribution.locate_file("reference_provider")).resolve())
        self.assertTrue(package.is_relative_to(Path(sys.prefix).resolve()))

        for name in ("science.py", "binding.py", "provider.py"):
            self.assertTrue(package.joinpath(name).is_file())
            self.assertIn(f"reference_provider/{name}", {str(path) for path in distribution.files})

    async def test_one_typed_query_call_has_no_analysis_or_state_lifetime(self):
        binding = await self.binding()
        call = binding.call(method=ReferenceMatcher.match)

        self.assertRegex(binding.description.binding_revision, r"\Aconfigured-v3:sha256:[0-9a-f]{64}\Z")
        self.assertEqual("match", call.description.name)
        self.assertIs(science.VectorInput, call.input_codec.value_type)
        self.assertIs(science.MatchReport, call.result_codec.value_type)
        self.assertIsNone(binding.contribution.analysis_type)
        self.assertEqual((), call.description.state_inputs)
        self.assertEqual((), call.description.state_outputs)
        self.assertEqual(1, next(iter(call.description.resources)).amount)

    async def test_startup_defers_index_building_to_the_instance_initializer(self):
        with patch("reference_provider.science.build_index", wraps=science.build_index) as build:
            binding = await self.binding()
            library = binding.contribution

            build.assert_not_called()
            self.assertIsNone(library.loaded_index)

    async def test_provider_settings_change_without_becoming_sdk_configuration(self):
        original = await self.binding()
        source = self.configuration.read_text(encoding="utf-8")

        self.configuration.write_text(data=source.replace("minimum_score = 0.5", "minimum_score = 0.9"), encoding="utf-8")
        changed_settings = await self.binding()

        self.configuration.write_text(data=source.replace("3.0, 4.0, 0.0, 0.0", "6.0, 4.0, 0.0, 0.0"), encoding="utf-8")
        changed_references = await self.binding()

        self.configuration.write_text(data="# Same values\n" + source, encoding="utf-8")
        reformatted = await self.binding()

        self.assertEqual(original.description.binding_revision, changed_settings.description.binding_revision)
        self.assertEqual(original.description.binding_revision, changed_references.description.binding_revision)
        self.assertEqual(original.description.binding_revision, reformatted.description.binding_revision)
        self.assertEqual(original.description.assets, changed_settings.description.assets)
        self.assertNotEqual(original.contribution.configuration, changed_references.contribution.configuration)
        self.assertEqual(0.5, original.contribution.configuration.settings.minimum_score)
        self.assertEqual((3, 4, 0, 0), original.contribution.configuration.references.entries[0].values)


if __name__ == "__main__":
    unittest.main()
