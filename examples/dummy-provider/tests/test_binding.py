"""Check the provider-specific binding and package boundary."""

from provider_core.container_guard import require_container

require_container()

from importlib import metadata, resources
from pathlib import Path
import os
import subprocess
import sys
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import provider_core.contribution as contribution
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import observe_cleanup
from dummy_provider import science
from dummy_provider import binding as binding_setup
from dummy_provider.binding import Sum, WeightedSum, WeightedSumConfig, read_resource_requirements


class DummyBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.configuration = directory / "provider.toml"

        shutil.copyfile(
            src=Path(__file__).parent / "config/deployments/localhost/provider.toml",
            dst=self.configuration,
        )

        self.enterContext(patch.object(target=binding_setup, attribute="PROVIDER_CONFIG_PATH", new=self.configuration))

        self.host = InProcessExecutionHost(
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

    def weighted_binding(self, *, settings, coefficients):
        return self.host.adopt(
            contribution=WeightedSum(configuration=WeightedSumConfig(settings=settings, coefficients=coefficients)),
            requirements=read_resource_requirements(),
        )

    def sum_binding(self):
        return self.host.adopt(contribution=Sum(), requirements=read_resource_requirements())

    async def test_installed_distribution_contains_the_scientific_package(self) -> None:
        installed_distribution = metadata.distribution("dummy-provider-example")
        installed_package = Path(resources.files("dummy_provider")).resolve()

        distribution_package = Path(
            installed_distribution.locate_file("dummy_provider")
        ).resolve()

        python_package_roots = {
            path.parts[0]
            for path in installed_distribution.files
            if path.suffix == ".py"
        }

        self.assertEqual(installed_package, distribution_package)
        self.assertTrue(installed_package.is_relative_to(Path(sys.prefix).resolve()))
        self.assertEqual({"dummy_provider"}, python_package_roots)
        self.assertTrue((installed_package / "science.py").is_file())
        self.assertTrue((installed_package / "binding.py").is_file())

    async def test_science_import_does_not_require_the_host_launcher(self) -> None:
        environment = os.environ.copy()

        if "NMR_PROVIDER_TOOLS_IN_CONTAINER" in environment:
            del environment["NMR_PROVIDER_TOOLS_IN_CONTAINER"]

        if "PROVIDER_CORE_IN_CONTAINER" in environment:
            del environment["PROVIDER_CORE_IN_CONTAINER"]

        scientific_import = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "dummy_provider.science",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(
            0,
            scientific_import.returncode,
            msg=scientific_import.stderr,
        )

    async def test_binding_preserves_provider_owned_types(self) -> None:
        weighted_binding = self.weighted_binding(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )

        (prepare_call, calculate_call) = (weighted_binding.call(method=WeightedSum.prepare), weighted_binding.call(method=WeightedSum.calculate))
        sum_call = self.sum_binding().call(method=Sum.run)

        self.assertRegex(weighted_binding.description.binding_revision, r"\Aconfigured-v3:sha256:[0-9a-f]{64}\Z")
        self.assertIs(science.PreparationInput, prepare_call.input_codec.value_type)
        self.assertIs(science.PreparedValues, calculate_call.state_binding.input_types["candidate"])
        self.assertIs(contribution.NoInput, calculate_call.input_codec.value_type)
        self.assertIs(science.SumInput, sum_call.input_codec.value_type)
        self.assertEqual("normalized", WeightedSum.normalized.name)
        self.assertEqual("candidate", WeightedSum.candidate.name)

    async def test_all_calls_declare_the_deployment_cpu_requirement(self) -> None:
        weighted = self.weighted_binding(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )

        expected = contribution.ResourceRequirement(
            kind="cpu", amount=1, unit="thread", selector="generic", exclusive=False,
        )

        for binding in (weighted, self.sum_binding()):
            for call in binding.description.calls:
                with self.subTest(binding=binding.description.binding_id, call=call.name):
                    self.assertEqual(frozenset({expected}), call.resources)

    async def test_resource_values_change_revision_without_changing_existing_bindings(self) -> None:
        weighted = self.weighted_binding(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )

        original_bindings = (weighted, self.sum_binding())
        configured = '[[execution.resource_requirement]]\nkind = "cpu"\namount = 2\nunit = "thread"\nselector = "generic"\nexclusive = false\n'

        with TemporaryDirectory() as directory:
            root = Path(directory)
            resource_file = root / "provider.toml"
            resource_file.write_text(configured, encoding="utf-8")

            with patch.object(target=binding_setup, attribute="PROVIDER_CONFIG_PATH", new=root / "provider.toml"):
                updated = self.weighted_binding(
                    settings=science.WeightedSumSettings(scale=2, offset=1),
                    coefficients=science.CoefficientSet(values=(1, 2, 3)),
                )

                updated_bindings = (updated, self.sum_binding())
                resource_file.write_text("# Same resource values\n" + configured, encoding="utf-8")

                reformatted = self.weighted_binding(
                    settings=science.WeightedSumSettings(scale=2, offset=1),
                    coefficients=science.CoefficientSet(values=(1, 2, 3)),
                )

                reformatted_bindings = (reformatted, self.sum_binding())

        for (original, changed, same_values) in zip(
            original_bindings, updated_bindings, reformatted_bindings, strict=True,
        ):
            with self.subTest(binding=original.description.binding_id):
                self.assertNotEqual(original.description.binding_revision, changed.description.binding_revision)
                self.assertEqual(changed.description.binding_revision, same_values.description.binding_revision)

                for call in original.description.calls:
                    self.assertEqual(1, next(iter(call.resources)).amount)

                for call in changed.description.calls:
                    self.assertEqual(2, next(iter(call.resources)).amount)

    async def test_invalid_resource_values_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)

            (root / "provider.toml").write_text(
                '[[execution.resource_requirement]]\nkind = "cpu"\namount = 0\nunit = "thread"\nselector = "generic"\nexclusive = false\n',
                encoding="utf-8",
            )

            with patch.object(target=binding_setup, attribute="PROVIDER_CONFIG_PATH", new=root / "provider.toml"):
                with self.assertRaisesRegex(ValueError, "Resource amount must be a positive integer"):
                    self.sum_binding()


if __name__ == "__main__":
    unittest.main()
