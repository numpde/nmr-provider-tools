# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/test_env_file_empty_defaults.py
# Complete original detector assertion groups; project input paths adapted locally.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _repo_surfaces
from ._repo_surfaces import (
    INTERPOLATION_DEFAULT_RE,
    REPO_ROOT,
    SILENT_DEFAULTS_PRIVATE_ENV_EXCLUSIONS,
    repo_path,
    surface_files,
    surface_lines,
)

ENV_FILE_GLOBS = ("*.env", "*.env.example", "**/*.env", "**/*.env.example")
ENV_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$")


def _env_files() -> list[Path]:
    return surface_files(
        ENV_FILE_GLOBS,
        exclude_repo_paths=SILENT_DEFAULTS_PRIVATE_ENV_EXCLUSIONS,
    )


class EnvFileEmptyDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_env_assignment_matcher_flags_blank_assignments(self) -> None:
        match = ENV_ASSIGNMENT_RE.match("OCI_TOKEN=")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual("", match.group("value"))
        self.assertEqual('""', ENV_ASSIGNMENT_RE.match('OCI_TOKEN=""').group("value"))
        self.assertEqual("''", ENV_ASSIGNMENT_RE.match("export OCI_TOKEN=''").group("value"))

    def test_env_interpolation_matcher_flags_default_expansion(self) -> None:
        self.assertRegex("API_ORIGIN=${API_ORIGIN:-https://example.test}", INTERPOLATION_DEFAULT_RE)

    def test_env_file_scan_excludes_operator_only_secret_files(self) -> None:
        # Original source proves a deployment-secret exclusion while admitting
        # its documented example. Exercise that same contract in a fixture:
        # NMRNet has no reviewed private dotenv exclusions to grant.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("private.env", "private.env.example"):
                (root / name).write_text("PROFILE=fixture\n")
            excluded = frozenset({Path("private.env")})
            with (
                patch.object(_repo_surfaces, "REPO_ROOT", root),
                patch(__name__ + ".SILENT_DEFAULTS_PRIVATE_ENV_EXCLUSIONS", excluded),
            ):
                scanned = {repo_path(path) for path in _env_files()}
            self.assertTrue(excluded.isdisjoint(scanned))
            self.assertIn(Path("private.env.example"), scanned)
            self.assertNotIn(Path("private.env"), scanned)

    def test_env_file_exclusions_must_be_repo_relative(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo-relative"):
            surface_files(
                ENV_FILE_GLOBS,
                exclude_repo_paths=frozenset({REPO_ROOT / "config/deploy/demo.env"}),
            )

    def test_surface_files_always_excludes_silent_defaults_tests(self) -> None:
        scanned = {repo_path(path) for path in surface_files(("tests/repository/**/*.py",))}

        self.assertNotIn(
            Path("tests/repository/quality/silent_defaults/test_env_file_empty_defaults.py"),
            scanned,
        )

    def test_env_files_do_not_publish_blank_assignments(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(_env_files(), skip_comments=True):
            stripped = line.strip()
            if not stripped:
                continue
            match = ENV_ASSIGNMENT_RE.match(stripped)
            if match and match.group("value").strip() in {"", '""', "''"}:
                findings.append(f"{source}:{line_number}: {match.group('name')}={match.group('value')}")

        self.assertEqual([], findings)

    def test_env_files_do_not_use_interpolation_defaults(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(_env_files(), skip_comments=True):
            for match in INTERPOLATION_DEFAULT_RE.finditer(line):
                findings.append(
                    f"{source}:{line_number}: "
                    f"{match.group('name')}{match.group('operator')}{match.group('value')!r}: {line.strip()}"
                )

        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
