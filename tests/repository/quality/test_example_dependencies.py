"""Keep standalone examples pinned to this SDK and its declared API capability."""

from tools.container_guard import require_container

require_container()

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.provider_core_tests import _discover_examples


class ExampleDependencyTests(unittest.TestCase):
    def test_native_and_api_examples_use_the_exact_candidate_sdk(self):
        for dependency in ("provider-core==0.6.19", "provider-core[api]==0.6.19"):
            with self.subTest(dependency=dependency), TemporaryDirectory() as directory:
                example = Path(directory) / "example"
                example.mkdir()

                (example / "pyproject.toml").write_text(data=(
                    '[project]\nname = "test-example"\nversion = "0.1.0"\n'
                    f'dependencies = ["{dependency}"]\n'
                ),
                    encoding="utf-8")

                self.assertEqual(
                    first=[(example, "test-example", "0.1.0")],
                    second=_discover_examples(examples=Path(directory), core_version="0.6.19"),
                )

    def test_other_versions_extras_and_provider_dependencies_remain_refused(self):
        for dependencies in (
            '["provider-core==0.6.18"]',
            '["provider-core[other]==0.6.19"]',
            '["provider-core[api,other]==0.6.19"]',
            '["provider-core>=0.6.19"]',
            '["provider-core[api]==0.6.19", "pydantic==2.14.3"]',
        ):
            with self.subTest(dependencies=dependencies), TemporaryDirectory() as directory:
                example = Path(directory) / "example"
                example.mkdir()

                (example / "pyproject.toml").write_text(data=(
                    '[project]\nname = "test-example"\nversion = "0.1.0"\n'
                    f'dependencies = {dependencies}\n'
                ),
                    encoding="utf-8")

                with self.assertRaises(expected_exception=ValueError):
                    _discover_examples(examples=Path(directory), core_version="0.6.19")
