"""Exercise explicit consumer file grants through the maintained checker launcher."""

from tools.container_guard import require_container

require_container()

import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.repository.quality import sources
from tests.repository.quality.test_validation_guard_posture import _CHECK_PATH, _scan


class CheckerSourceSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)
        self.consumer_root = pathlib.Path(temporary.name)

        for consumer_relative_path in ["consumer/adapter.py", "tests/test_adapter.py", "consumer/unselected.py", "secrets/key.py"]:
            consumer_file = self.consumer_root / consumer_relative_path

            consumer_file.parent.mkdir(parents=True, exist_ok=True)
            consumer_file.write_text("pass\n")

        self.environment = os.environ | {
            "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///tmp/checker-selection.sock",
            "TEST_PATTERN_INPUT_ORIGIN": "command line",
            "TEST_PATTERN_INPUT": "test_negated_call_parentheses_posture.py",
            "CHECK_SOURCE_ROOT_INPUT_ORIGIN": "command line",
            "CHECK_SOURCE_ROOT_INPUT": str(self.consumer_root),
            "CHECK_SOURCE_FILES_INPUT_ORIGIN": "command line",
            "CHECK_SOURCE_FILES_INPUT": "consumer/adapter.py tests/test_adapter.py",
        }

    def preview(self, **overrides):
        return subprocess.run(
            ["bash", str(sources.CHECKER_ROOT / "tests/repository/run.sh"), "--print", "tests/repository/quality"],
            env=self.environment | overrides,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )

    def test_only_selected_consumer_files_receive_read_only_mounts(self):
        checker_preview = self.preview()

        self.assertEqual(0, checker_preview.returncode, msg=checker_preview.stderr)

        arguments = shlex.split(checker_preview.stdout)
        consumer_mounts = [argument for argument in arguments if argument.startswith(f"type=bind,src={self.consumer_root}")]

        self.assertEqual(
            [
                f"type=bind,src={self.consumer_root}/consumer/adapter.py,dst=/selected-source/consumer/adapter.py,readonly",
                f"type=bind,src={self.consumer_root}/tests/test_adapter.py,dst=/selected-source/tests/test_adapter.py,readonly",
            ],
            consumer_mounts,
        )

        self.assertIn("CHECK_SOURCE_ROOT=/selected-source", arguments)
        self.assertIn("--read-only", arguments)
        self.assertIn("--network=none", arguments)

    def test_private_missing_traversing_and_symlinked_files_are_refused_before_dispatch(self):
        (self.consumer_root / "linked").symlink_to(self.consumer_root / "consumer", target_is_directory=True)

        for file_selection in [
            "secrets/key.py",
            ".aws/key.py",
            ".codex/key.py",
            "../adapter.py",
            "/tmp/adapter.py",
            "consumer/missing.py",
            "linked/adapter.py",
            "",
            "consumer/adapter.py\n"
            "secrets/key.py",
        ]:
            with self.subTest(file_selection=file_selection):
                checker_preview = self.preview(CHECK_SOURCE_FILES_INPUT=file_selection)

                self.assertEqual(2, checker_preview.returncode, msg=checker_preview.stderr)
                self.assertEqual("", checker_preview.stdout)

        private_root_preview = self.preview(
            CHECK_SOURCE_ROOT_INPUT=str(self.consumer_root / "secrets"),
            CHECK_SOURCE_FILES_INPUT="key.py",
        )

        self.assertEqual(2, private_root_preview.returncode, msg=private_root_preview.stderr)
        self.assertEqual("", private_root_preview.stdout)

    def test_both_source_arguments_and_a_focused_detector_are_required(self):
        for missing_origin in ["CHECK_SOURCE_ROOT_INPUT_ORIGIN", "CHECK_SOURCE_FILES_INPUT_ORIGIN", "TEST_PATTERN_INPUT_ORIGIN"]:
            with self.subTest(missing_origin=missing_origin):
                checker_preview = self.preview(**{missing_origin: "undefined"})

                self.assertEqual(2, checker_preview.returncode, msg=checker_preview.stderr)
                self.assertEqual("", checker_preview.stdout)

    def test_root_control_characters_cannot_select_a_different_directory(self):
        newline_root = self.consumer_root.with_name(self.consumer_root.name + "\n")

        newline_root.mkdir()
        self.addCleanup(newline_root.rmdir)

        checker_preview = self.preview(CHECK_SOURCE_ROOT_INPUT=str(newline_root))

        self.assertEqual(2, checker_preview.returncode, msg=checker_preview.stderr)
        self.assertEqual("", checker_preview.stdout)

        root_link = self.consumer_root / "root-link"

        root_link.symlink_to(newline_root, target_is_directory=True)

        linked_root_preview = self.preview(CHECK_SOURCE_ROOT_INPUT=str(root_link))

        self.assertEqual(2, linked_root_preview.returncode, msg=linked_root_preview.stderr)
        self.assertEqual("", linked_root_preview.stdout)

    def test_ambient_source_arguments_do_not_grant_consumer_reads(self):
        checker_preview = self.preview(CHECK_SOURCE_ROOT_INPUT_ORIGIN="environment", CHECK_SOURCE_FILES_INPUT_ORIGIN="environment")

        self.assertEqual(0, checker_preview.returncode, msg=checker_preview.stderr)
        self.assertNotIn("/selected-source", checker_preview.stdout)

    def test_explicit_projection_includes_arbitrary_package_names_and_selected_tests(self):
        (self.consumer_root / "consumer/unselected.py").unlink()

        with patch.object(sources, "ROOT", self.consumer_root), patch.object(sources, "EXPLICIT_SOURCE_SELECTION", True):
            for production_only in [False, True]:
                with self.subTest(production_only=production_only):
                    selected_paths = sources.python_sources(production_only=production_only)

                    self.assertEqual(
                        ["consumer/adapter.py", "tests/test_adapter.py"],
                        [source_path.relative_to(self.consumer_root).as_posix() for source_path in selected_paths],
                    )

    def test_detector_self_audit_and_consumer_findings_use_their_own_roots(self):
        consumer_file = self.consumer_root / "consumer/adapter.py"

        consumer_file.write_text(
            "if size > ceiling:\n"
            "    raise ValueError('outside bounds')\n"
        )

        self.assertEqual([], _scan([_CHECK_PATH], source_root=sources.CHECKER_ROOT))

        consumer_findings = _scan([consumer_file], source_root=self.consumer_root)

        self.assertEqual(1, len(consumer_findings))
        self.assertEqual(pathlib.Path("consumer/adapter.py"), consumer_findings[0].source_path)
        self.assertEqual(1, consumer_findings[0].line)
