# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/test_compose_empty_defaults.py
# Complete original detector assertion groups; project input paths adapted locally.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import unittest

from ._repo_surfaces import INTERPOLATION_DEFAULT_RE, surface_files, surface_lines

COMPOSE_SURFACE_GLOBS = (
    "compose/*.yml",
    "compose/*.yaml",
    "tools/**/compose*.yml",
    "tools/**/compose*.yaml",
)


class ComposeEmptyDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_compose_matcher_flags_empty_default_interpolation(self) -> None:
        self.assertRegex("${POSTGRES_IMAGE_REF-postgres:16}", INTERPOLATION_DEFAULT_RE)
        self.assertRegex("${POSTGRES_IMAGE_REF:-postgres:16}", INTERPOLATION_DEFAULT_RE)
        self.assertRegex("${POSTGRES_IMAGE_REF=postgres:16}", INTERPOLATION_DEFAULT_RE)
        self.assertRegex("${POSTGRES_IMAGE_REF:=postgres:16}", INTERPOLATION_DEFAULT_RE)

    def test_compose_matcher_ignores_strict_required_interpolation(self) -> None:
        self.assertNotRegex("${POSTGRES_IMAGE_REF:?missing_POSTGRES_IMAGE_REF}", INTERPOLATION_DEFAULT_RE)

    def test_compose_files_use_strict_interpolation_not_empty_defaults(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(
            surface_files(COMPOSE_SURFACE_GLOBS),
            skip_comments=True,
        ):
            for match in INTERPOLATION_DEFAULT_RE.finditer(line):
                findings.append(
                    f"{source}:{line_number}: "
                    f"{match.group('name')}{match.group('operator')}{match.group('value')!r}: {line.strip()}"
                )

        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
