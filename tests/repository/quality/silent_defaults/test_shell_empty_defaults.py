# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/test_shell_empty_defaults.py
# Complete original detector assertion groups; project input paths adapted locally.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _repo_surfaces
from ._repo_surfaces import (
    INTERPOLATION_DEFAULT_RE,
    SILENT_DEFAULTS_PRIVATE_ENV_EXCLUSIONS,
    repo_path,
    surface_files,
    surface_lines,
)

SHELL_SURFACE_GLOBS = (
    "Makefile",
    "*.mk",
    "*.py",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "compose/**/*.yml",
    "compose/**/*.yaml",
    "config/**/*.env",
    "config/**/*.env.example",
    "containers/**/Dockerfile*",
    "containers/**/*.sh",
    "tools/**/*.py",
    "tools/**/Dockerfile",
    "tools/**/*.mk",
    "tools/**/*.sh",
    "src/**/*.py",
    "packages/*/src/**/*.py",
)


def _shell_surface_files() -> list[Path]:
    return surface_files(
        SHELL_SURFACE_GLOBS,
        extra_exclude_parts=frozenset({"tests"}),
        exclude_repo_paths=SILENT_DEFAULTS_PRIVATE_ENV_EXCLUSIONS,
    )


class ShellEmptyDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_shell_matcher_flags_empty_default_expansion(self) -> None:
        self.assertRegex('profile="${PROFILE-demo}"', INTERPOLATION_DEFAULT_RE)
        self.assertRegex('profile="${PROFILE:-demo}"', INTERPOLATION_DEFAULT_RE)
        self.assertRegex('first="${1:-demo}"', INTERPOLATION_DEFAULT_RE)
        self.assertRegex('all="${@:-demo}"', INTERPOLATION_DEFAULT_RE)
        self.assertRegex('path="${DEPLOY_LOCK_FILE=.deploy.lock}"', INTERPOLATION_DEFAULT_RE)
        self.assertRegex('path="${DEPLOY_LOCK_FILE:=.deploy.lock}"', INTERPOLATION_DEFAULT_RE)

    def test_shell_matcher_ignores_strict_required_expansion(self) -> None:
        self.assertNotRegex("${PROFILE:?missing_PROFILE}", INTERPOLATION_DEFAULT_RE)

    def test_shell_surface_scan_excludes_operator_only_secret_files(self) -> None:
        # No local secret exemption: retain the original secret/example
        # classification proof using temporary deployment input fixtures.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            for name in ("private.env", "private.env.example"):
                (config / name).write_text("PROFILE=fixture\n")
            excluded = frozenset({Path("config/private.env")})
            with (
                patch.object(_repo_surfaces, "REPO_ROOT", root),
                patch(__name__ + ".SILENT_DEFAULTS_PRIVATE_ENV_EXCLUSIONS", excluded),
            ):
                scanned = {repo_path(path) for path in _shell_surface_files()}
            self.assertTrue(excluded.isdisjoint(scanned))
            self.assertIn(Path("config/private.env.example"), scanned)
            self.assertNotIn(Path("config/private.env"), scanned)

    def test_shell_operator_surfaces_do_not_use_empty_default_expansion(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(_shell_surface_files(), skip_comments=True):
            for match in INTERPOLATION_DEFAULT_RE.finditer(line):
                findings.append(
                    f"{source}:{line_number}: "
                    f"{match.group('name')}{match.group('operator')}{match.group('value')!r}: {line.strip()}"
                )

        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
