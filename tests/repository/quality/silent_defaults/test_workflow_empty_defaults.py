# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/test_workflow_empty_defaults.py
# Complete original detector assertion groups; project input paths adapted locally.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import unittest
from pathlib import Path

from ._repo_surfaces import repo_source, surface_files, surface_lines

WORKFLOW_GLOBS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)
WORKFLOW_DEFAULT_FIELD_RE = re.compile(r"^\s*default:\s*(?P<value>.*)$")
WORKFLOW_MAPPING_KEY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*):\s*(?P<value>.*)$")
WORKFLOW_ENV_EMPTY_RE = re.compile(r"^(?P<indent>\s*)[A-Za-z_][A-Za-z0-9_]*:\s*(?:''|\"\")?\s*$")


def _workflow_blank_env_entries_from_lines(lines: list[str], source: str) -> list[str]:
    findings: list[str] = []
    yaml_stack: list[tuple[int, str]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        key_match = WORKFLOW_MAPPING_KEY_RE.match(line)
        if key_match:
            indent = len(key_match.group("indent"))
            while yaml_stack and yaml_stack[-1][0] >= indent:
                yaml_stack.pop()

            parent_keys = [key for _, key in yaml_stack]
            if "env" in parent_keys and WORKFLOW_ENV_EMPTY_RE.match(line):
                findings.append(f"{source}:{line_number}: {line.strip()}")

            if key_match.group("value") == "":
                yaml_stack.append((indent, key_match.group("key")))

    return findings


def _workflow_blank_env_entries(path: Path) -> list[str]:
    return _workflow_blank_env_entries_from_lines(
        path.read_text(encoding="utf-8").splitlines(),
        repo_source(path),
    )


class WorkflowEmptyDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_workflow_matcher_flags_default_fields(self) -> None:
        self.assertRegex("      default: demo", WORKFLOW_DEFAULT_FIELD_RE)
        self.assertRegex("      default:", WORKFLOW_DEFAULT_FIELD_RE)

    def test_workflow_blank_env_scanner_only_flags_entries_inside_env_blocks(self) -> None:
        findings = _workflow_blank_env_entries_from_lines(
            [
                "on:",
                "jobs:",
                "  build:",
                "    env:",
                "      OCI_TOKEN:",
                '      OCI_USERNAME: ""',
                "    steps:",
                "      - run: true",
            ],
            "workflow.yml",
        )

        self.assertEqual(
            ["workflow.yml:5: OCI_TOKEN:", 'workflow.yml:6: OCI_USERNAME: ""'],
            findings,
        )

    def test_workflows_do_not_define_dispatch_defaults(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(surface_files(WORKFLOW_GLOBS)):
            match = WORKFLOW_DEFAULT_FIELD_RE.match(line)
            if match:
                findings.append(f"{source}:{line_number}: default: {match.group('value')!r}")

        self.assertEqual([], findings)

    def test_workflows_do_not_publish_blank_env_entries(self) -> None:
        findings: list[str] = []
        for path in surface_files(WORKFLOW_GLOBS):
            findings.extend(_workflow_blank_env_entries(path))

        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
