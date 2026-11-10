"""Check the provider-specific binding and package boundary."""

from provider_core.container_guard import require_container

require_container()

from importlib import metadata, resources
from pathlib import Path
import os
import subprocess
import sys
import unittest

import provider_core.contribution as contribution
from dummy_provider import science
from dummy_provider.binding import create_sum_binding, create_weighted_sum_binding


class DummyBindingTests(unittest.TestCase):
    def test_installed_distribution_contains_the_scientific_package(self) -> None:
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

    def test_science_import_does_not_require_the_host_launcher(self) -> None:
        environment = os.environ.copy()
        environment.pop("NMR_PROVIDER_TOOLS_IN_CONTAINER", None)
        environment.pop("PROVIDER_CORE_IN_CONTAINER", None)

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

    def test_binding_preserves_provider_owned_types(self) -> None:
        weighted_binding = create_weighted_sum_binding(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )
        (prepare_call, calculate_call) = weighted_binding.calls
        (sum_call,) = create_sum_binding().calls

        self.assertIs(science.PreparationInput, prepare_call.input_codec.value_type)
        self.assertIs(science.CalculationState, calculate_call.state_binding.value_type)
        self.assertIs(contribution.NoInput, calculate_call.input_codec.value_type)
        self.assertIs(science.SumInput, sum_call.input_codec.value_type)


if __name__ == "__main__":
    unittest.main()
