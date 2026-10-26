# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/test_make_empty_defaults.py
# Complete original detector assertion groups; project input paths adapted locally.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import unittest

from ._repo_surfaces import surface_files, surface_lines
from .test_shared_scanner import is_literal_make_origin_gate

MAKE_SURFACE_GLOBS = (
    "Makefile",
    "*.mk",
    "tools/**/*.mk",
)
MAKE_DEFAULT_ASSIGNMENT_RE = re.compile(r"^(?:export\s+|override\s+)?(?P<name>[A-Za-z0-9_./-]+)\s*\?=\s*(?P<value>.*)$")
MAKE_FUNCTION_DEFAULT_RE = re.compile(r"\$\((?:or|if)\b[^)]*\)")
MAKE_EMPTY_CHECK_ASSIGNMENT_RE = re.compile(
    r"\bif\s+\[\[\s+-z\s+\"\$\$(?P<checked>[A-Za-z_][A-Za-z0-9_]*)\"\s+\]\];\s+then\s+"
    r"(?P<assigned>[A-Za-z_][A-Za-z0-9_]*)="
)
MAKE_ASSIGNMENT_OPERATOR_RE = r"(?::=|::=|:::=|\+=|\?=|!=|=)"
MAKE_TARGET_SPECIFIC_VARIABLE_RE = re.compile(
    r"^(?P<targets>[^#:=\s][^#:=]*?)\s*:\s*"
    r"(?:(?:export|private|override)\s+)*"
    rf"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*{MAKE_ASSIGNMENT_OPERATOR_RE}"
)
MAKE_GLOBAL_VARIABLE_RE = re.compile(rf"^(?:(?:export|override)\s+)?[A-Za-z_][A-Za-z0-9_./-]+\s*{MAKE_ASSIGNMENT_OPERATOR_RE}")


def _make_surface_lines():
    return surface_lines(surface_files(MAKE_SURFACE_GLOBS), skip_comments=True)


def make_function_default_findings():
    """Retain raw Make fallback matches except complete literal origin gates."""
    findings = []
    for source, line_number, line in _make_surface_lines():
        if is_literal_make_origin_gate(source, line):
            continue
        for match in MAKE_FUNCTION_DEFAULT_RE.finditer(line):
            findings.append(f"{source}:{line_number}: {match.group(0)}")
    return findings


class MakeEmptyDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_make_matcher_flags_conditional_assignment_defaults(self) -> None:
        self.assertRegex("PROFILE ?= demo", MAKE_DEFAULT_ASSIGNMENT_RE)
        self.assertRegex("export PROFILE ?= demo", MAKE_DEFAULT_ASSIGNMENT_RE)
        self.assertRegex("override PROFILE ?= demo", MAKE_DEFAULT_ASSIGNMENT_RE)
        self.assertRegex("OCI_TOKEN ?=", MAKE_DEFAULT_ASSIGNMENT_RE)

    def test_make_matcher_flags_function_fallbacks(self) -> None:
        self.assertRegex("$(or $(PROFILE),demo)", MAKE_FUNCTION_DEFAULT_RE)
        self.assertRegex("$(if $(PROFILE),$(PROFILE),demo)", MAKE_FUNCTION_DEFAULT_RE)

    def test_make_matcher_flags_empty_check_assignments(self) -> None:
        self.assertRegex(
            'if [[ -z "$$profile" ]]; then profile="demo"; fi; \\',
            MAKE_EMPTY_CHECK_ASSIGNMENT_RE,
        )

    def test_make_matcher_flags_target_specific_variables(self) -> None:
        self.assertRegex("deploy-prod: PROFILE=prod", MAKE_TARGET_SPECIFIC_VARIABLE_RE)
        self.assertRegex(
            "deploy-demo deploy-prod: PROFILE=demo",
            MAKE_TARGET_SPECIFIC_VARIABLE_RE,
        )
        for assignment in (
            "deploy-prod: export PROFILE=prod",
            "deploy-prod: private PROFILE=prod",
            "deploy-prod: override PROFILE=prod",
            "deploy-prod: PROFILE:=prod",
            "deploy-prod: PROFILE ::= prod",
            "deploy-prod: PROFILE :::= prod",
            "deploy-prod: PROFILE += prod",
            "deploy-prod: PROFILE ?= prod",
            "deploy-prod: PROFILE != printf prod",
        ):
            self.assertRegex(assignment, MAKE_TARGET_SPECIFIC_VARIABLE_RE)
        self.assertNotRegex("PROFILE := prod", MAKE_TARGET_SPECIFIC_VARIABLE_RE)
        self.assertNotRegex("override PROFILE := prod", MAKE_TARGET_SPECIFIC_VARIABLE_RE)

    def test_make_surfaces_do_not_use_conditional_assignment_defaults(self) -> None:
        findings: list[str] = []
        for source, line_number, line in _make_surface_lines():
            match = MAKE_DEFAULT_ASSIGNMENT_RE.match(line)
            if match:
                findings.append(f"{source}:{line_number}: {match.group('name')} ?= {match.group('value')!r}")

        self.assertEqual([], findings)

    def test_make_surfaces_do_not_use_function_fallbacks(self) -> None:
        self.assertEqual([], make_function_default_findings())

    def test_make_surfaces_do_not_assign_defaults_after_empty_checks(self) -> None:
        findings: list[str] = []
        for source, line_number, line in _make_surface_lines():
            match = MAKE_EMPTY_CHECK_ASSIGNMENT_RE.search(line)
            if match and match.group("checked") == match.group("assigned"):
                findings.append(f"{source}:{line_number}: {line.strip()}")

        self.assertEqual([], findings)

    def test_make_surfaces_do_not_use_target_specific_variables(self) -> None:
        findings: list[str] = []
        for source, line_number, line in _make_surface_lines():
            if line.startswith(("\t", " ")):
                continue
            stripped_line = line.strip()
            if not stripped_line or MAKE_GLOBAL_VARIABLE_RE.match(stripped_line):
                continue
            match = MAKE_TARGET_SPECIFIC_VARIABLE_RE.match(stripped_line)
            if match:
                findings.append(f"{source}:{line_number}: {match.group('name')} on {match.group('targets')!r}")

        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
