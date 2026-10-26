# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/test_dockerfile_empty_defaults.py
# Complete original detector assertion groups; project input paths adapted locally.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import unittest

from ._repo_surfaces import surface_files, surface_lines

DOCKERFILE_GLOBS = (
    "**/Dockerfile",
    "**/*.Dockerfile",
    "containers/**/Dockerfile.lock",
)
DOCKER_ARG_DEFAULT_RE = re.compile(r"^\s*ARG\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:=(?P<value>.*))?\s*$")
DOCKER_ENV_ASSIGNMENT_RE = re.compile(r"^\s*ENV\s+(?P<body>.+)$")
DOCKER_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DOCKER_EMPTY_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?:''|\"\")?$")
EMPTY_QUOTED_VALUE_TOKENS = frozenset({"''", '""'})


def _blank_docker_env_entries(body: str) -> list[str]:
    tokens = body.split()
    findings = [token for token in tokens if DOCKER_EMPTY_ENV_ASSIGNMENT_RE.match(token)]
    if len(tokens) == 2 and DOCKER_ENV_NAME_RE.match(tokens[0]) and tokens[1] in EMPTY_QUOTED_VALUE_TOKENS:
        findings.append(f"{tokens[0]} {tokens[1]}")
    return findings


class DockerfileEmptyDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_docker_arg_matcher_flags_defaults(self) -> None:
        match = DOCKER_ARG_DEFAULT_RE.match("ARG OCI_NAMESPACE=example-owner/vote-mcp")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual("example-owner/vote-mcp", match.group("value"))

    def test_docker_env_matcher_flags_empty_values(self) -> None:
        self.assertEqual(["OCI_TOKEN="], _blank_docker_env_entries("OCI_TOKEN="))
        self.assertEqual(['OCI_TOKEN=""'], _blank_docker_env_entries('OCI_TOKEN=""'))
        self.assertEqual(["OCI_TOKEN=''"], _blank_docker_env_entries("OCI_TOKEN=''"))
        self.assertEqual(['OCI_TOKEN ""'], _blank_docker_env_entries('OCI_TOKEN ""'))
        self.assertEqual(["OCI_TOKEN ''"], _blank_docker_env_entries("OCI_TOKEN ''"))
        self.assertEqual([], _blank_docker_env_entries('OCI_TOKEN "" extra'))

    def test_dockerfiles_do_not_define_arg_defaults(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(
            surface_files(DOCKERFILE_GLOBS),
            skip_comments=True,
        ):
            match = DOCKER_ARG_DEFAULT_RE.match(line)
            if match and match.group("value") is not None:
                findings.append(f"{source}:{line_number}: ARG {match.group('name')}={match.group('value')!r}")

        self.assertEqual([], findings)

    def test_dockerfiles_do_not_publish_blank_env_values(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(
            surface_files(DOCKERFILE_GLOBS),
            skip_comments=True,
        ):
            match = DOCKER_ENV_ASSIGNMENT_RE.match(line)
            if not match:
                continue
            for entry in _blank_docker_env_entries(match.group("body")):
                findings.append(f"{source}:{line_number}: ENV {entry}")

        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
