"""Keep the dummy provider limited to science and its contribution adapter."""

from importlib import resources
import os
import subprocess
import sys
import unittest

from provider_core.contribution import ScientificRejection as ExportedScientificRejection
from provider_core.contribution_errors import ScientificRejection
import provider_core.examples.dummy_provider as dummy_provider
from provider_core.examples.dummy_provider import science


class DummyProviderBoundaryTests(unittest.TestCase):
    def test_provider_contains_only_science_and_adapter(self):
        root = resources.files(dummy_provider)
        sources = {item.name for item in root.iterdir() if item.name.endswith(".py")}

        self.assertEqual({"__init__.py", "binding.py", "science.py"}, sources)

    def test_science_import_does_not_require_the_runtime_launcher(self):
        environment = os.environ.copy()
        environment.pop("PROVIDER_CORE_IN_CONTAINER", None)

        import_check = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from provider_core.examples.dummy_provider import science; "
                    "science.WeightedSumSettings(scale=1, offset=0)"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(0, import_check.returncode, import_check.stderr)

    def test_scientific_rejection_uses_the_lightweight_shared_contract(self):
        self.assertIs(ScientificRejection, ExportedScientificRejection)
        self.assertTrue(
            issubclass(
                science.ScientificInputRejected,
                ScientificRejection,
            )
        )


if __name__ == "__main__":
    unittest.main()
