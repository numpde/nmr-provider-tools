"""Keep examples on the candidate SDK and their own locked scientific dependencies."""

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

    def test_other_sdk_versions_and_extras_remain_refused(self):
        for dependencies in (
            '["provider-core==0.6.18"]',
            '["provider-core[other]==0.6.19"]',
            '["provider-core[api,other]==0.6.19"]',
            '["provider-core>=0.6.19"]',
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

    def test_scientific_dependencies_require_matching_hashed_lock(self):
        with TemporaryDirectory() as directory:
            example = Path(directory) / "example"

            example.mkdir()

            (example / "pyproject.toml").write_text(
                data=(
                    '[project]\nname = "test-example"\nversion = "0.1.0"\n'
                    'dependencies = ["provider-core==0.6.19", "numpy==2.5.4"]\n'
                ),
                encoding="utf-8",
            )

            with self.assertRaises(expected_exception=FileNotFoundError):
                _discover_examples(examples=Path(directory), core_version="0.6.19")

            (example / "requirements.lock").write_text(data="numpy==2.5.4 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")

            self.assertEqual(
                first=[(example, "test-example", "0.1.0")],
                second=_discover_examples(examples=Path(directory), core_version="0.6.19"),
            )

            with (example / "requirements.lock").open(mode="a", encoding="utf-8") as scientific_lock_file:
                scientific_lock_file.write("transitive-package==1.0.0 \\\n    --hash=sha256:" + "1" * 64 + "\n")

            self.assertEqual(
                first=[(example, "test-example", "0.1.0")],
                second=_discover_examples(examples=Path(directory), core_version="0.6.19"),
            )

            (example / "requirements.lock").write_text(data="numpy==2.4.1 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")

            with self.assertRaises(expected_exception=ValueError):
                _discover_examples(examples=Path(directory), core_version="0.6.19")

    def test_orphan_scientific_lock_and_sdk_replacement_are_refused(self):
        for dependencies in [
            '["provider-core==0.6.19"]',
            '["provider-core==0.6.19", "provider-core==0.6.18"]',
        ]:
            with self.subTest(dependencies=dependencies), TemporaryDirectory() as directory:
                example = Path(directory) / "example"

                example.mkdir()

                (example / "pyproject.toml").write_text(
                    data=(
                        '[project]\nname = "test-example"\nversion = "0.1.0"\n'
                        f'dependencies = {dependencies}\n'
                    ),
                    encoding="utf-8",
                )

                (example / "requirements.lock").write_text(data="provider-core==0.6.18 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")

                with self.assertRaises(expected_exception=ValueError):
                    _discover_examples(examples=Path(directory), core_version="0.6.19")
