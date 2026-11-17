"""Keep examples on the candidate SDK and their own locked scientific dependencies."""

from tools.container_guard import require_container

require_container()

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.provider_core_tests import _discover_examples, _discover_mocks


class ExampleDependencyTests(unittest.TestCase):
    def test_native_and_api_examples_use_the_exact_candidate_sdk(self):
        for dependency in ("provider-core==0.6.19", "provider-core[api]==0.6.19"):
            with self.subTest(dependency=dependency), TemporaryDirectory() as directory:
                example = Path(directory) / "example"

                example.mkdir()

                (example / "pyproject.toml").write_text(
                    data=(
                    f'[project]\nname = "test-example"\nversion = "0.1.0"\n'
                    f'dependencies = ["{dependency}"]\n'
                ),
                    encoding="utf-8"
                )

                self.assertEqual(
                    first=[(example, "test-example", "0.1.0")],
                    second=_discover_examples(examples=Path(directory), core_version="0.6.19", mock_dependencies=frozenset()),
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

                (example / "pyproject.toml").write_text(
                    data=(
                    f'[project]\nname = "test-example"\nversion = "0.1.0"\n'
                    f'dependencies = {dependencies}\n'
                ),
                    encoding="utf-8"
                )

                with self.assertRaises(expected_exception=ValueError):
                    _discover_examples(examples=Path(directory), core_version="0.6.19", mock_dependencies=frozenset())

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
                _discover_examples(examples=Path(directory), core_version="0.6.19", mock_dependencies=frozenset())

            (example / "requirements.lock").write_text(data="numpy==2.5.4 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")

            self.assertEqual(
                first=[(example, "test-example", "0.1.0")],
                second=_discover_examples(examples=Path(directory), core_version="0.6.19", mock_dependencies=frozenset()),
            )

            with (example / "requirements.lock").open(mode="a", encoding="utf-8") as scientific_lock_file:
                scientific_lock_file.write("transitive-package==1.0.0 \\\n    --hash=sha256:" + "1" * 64 + "\n")

            self.assertEqual(
                first=[(example, "test-example", "0.1.0")],
                second=_discover_examples(examples=Path(directory), core_version="0.6.19", mock_dependencies=frozenset()),
            )

            (example / "requirements.lock").write_text(data="numpy==2.4.1 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")

            with self.assertRaises(expected_exception=ValueError):
                _discover_examples(examples=Path(directory), core_version="0.6.19", mock_dependencies=frozenset())

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
                        f'[project]\nname = "test-example"\nversion = "0.1.0"\n'
                        f'dependencies = {dependencies}\n'
                    ),
                    encoding="utf-8",
                )

                (example / "requirements.lock").write_text(data="provider-core==0.6.18 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")

                with self.assertRaises(expected_exception=ValueError):
                    _discover_examples(examples=Path(directory), core_version="0.6.19", mock_dependencies=frozenset())

    def test_repository_mock_pin_needs_no_external_wheel_lock(self):
        with TemporaryDirectory() as directory:
            example = Path(directory) / "example"

            example.mkdir()

            (example / "pyproject.toml").write_text(
                data=(
                    '[project]\nname = "test-example"\nversion = "0.1.0"\n'
                    'dependencies = ["provider-core==0.6.19", "mock-numpy==0.1.0"]\n'
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                first=[(example, "test-example", "0.1.0")],
                second=_discover_examples(
                    examples=Path(directory),
                    core_version="0.6.19",
                    mock_dependencies=frozenset({"mock-numpy==0.1.0"}),
                ),
            )

            with self.assertRaises(expected_exception=FileNotFoundError):
                _discover_examples(
                    examples=Path(directory),
                    core_version="0.6.19",
                    mock_dependencies=frozenset({"mock-numpy==0.2.0"}),
                )

    def test_external_lock_cannot_replace_a_repository_mock(self):
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

            (example / "requirements.lock").write_text(
                data=(
                    "numpy==2.5.4 \\\n    --hash=sha256:" + "0" * 64 + "\n"
                    "mock-numpy==0.1.0 \\\n    --hash=sha256:" + "1" * 64 + "\n"
                ),
                encoding="utf-8",
            )

            with self.assertRaises(expected_exception=ValueError):
                _discover_examples(
                    examples=Path(directory),
                    core_version="0.6.19",
                    mock_dependencies=frozenset({"mock-numpy==0.1.0"}),
                )

    def test_mock_projects_are_dependency_free_and_cannot_replace_the_sdk(self):
        with TemporaryDirectory() as directory:
            scientific_mock = Path(directory) / "mock_numpy"

            scientific_mock.mkdir()

            for (distribution_name, dependencies) in (
                ("mock-numpy", "[]"),
                ("mock-numpy", '["provider-core==0.6.19"]'),
                ("provider-core", "[]"),
            ):
                with self.subTest(distribution_name=distribution_name, dependencies=dependencies):
                    (scientific_mock / "pyproject.toml").write_text(
                        data=(
                            f'[project]\nname = "{distribution_name}"\nversion = "0.1.0"\n'
                            f'dependencies = {dependencies}\n'
                        ),
                        encoding="utf-8",
                    )

                    if distribution_name == "mock-numpy" and dependencies == "[]":
                        self.assertEqual(
                            first=[(scientific_mock, "mock-numpy", "0.1.0")],
                            second=_discover_mocks(mocks_root=Path(directory)),
                        )

                        (scientific_mock / "requirements.lock").write_text(data="provider-core==0.6.18\n", encoding="utf-8")

                        with self.assertRaises(expected_exception=ValueError):
                            _discover_mocks(mocks_root=Path(directory))

                        (scientific_mock / "requirements.lock").unlink()
                    else:
                        with self.assertRaises(expected_exception=ValueError):
                            _discover_mocks(mocks_root=Path(directory))
