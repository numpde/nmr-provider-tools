"""Check the provider-specific binding and package boundary."""

from provider_core.container_guard import require_container

require_container()

from importlib import resources
import os
import subprocess
import sys
import unittest

import provider_core.contribution as contribution
from dummy_provider import science
from dummy_provider.binding import create_sum_binding, create_weighted_sum_binding


class DummyBindingTests(unittest.TestCase):
    def test_runtime_package_contains_only_science_and_binding(self) -> None:
        package_root = resources.files("dummy_provider")
        python_files = {
            item.name
            for item in package_root.iterdir()
            if item.name.endswith(".py")
        }

        self.assertEqual(
            {
                "__init__.py",
                "binding.py",
                "science.py",
            },
            python_files,
        )

    def test_science_import_does_not_require_the_host_launcher(self) -> None:
        environment = os.environ.copy()
        environment.pop("NMR_PROVIDER_TOOLS_IN_CONTAINER", None)
        environment.pop("PROVIDER_CORE_IN_CONTAINER", None)

        import_check = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                (
                    "from dummy_provider import science; "
                    "science.WeightedSumSettings(scale=1, offset=0)"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(
            0,
            import_check.returncode,
            msg=import_check.stderr,
        )

    def test_binding_preserves_provider_owned_types(self) -> None:
        weighted = create_weighted_sum_binding(
            settings=science.WeightedSumSettings(
                scale=2,
                offset=1,
            ),
            coefficients=science.CoefficientSet(
                values=(1, 2, 3),
            ),
        )
        (prepare, calculate) = weighted.calls
        run = create_sum_binding().calls[0]

        self.assertIs(
            science.PreparationInput,
            prepare.input_codec.value_type,
        )
        self.assertIs(
            science.CalculationState,
            calculate.state_binding.value_type,
        )
        self.assertIs(
            contribution.NoInput,
            calculate.input_codec.value_type,
        )
        self.assertIs(
            science.SumInput,
            run.input_codec.value_type,
        )


if __name__ == "__main__":
    unittest.main()
