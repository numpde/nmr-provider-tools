# Port source: https://github.com/numpde/dapp32/blob/778c16f8d8b77181d2ac4cd11a62a2034ded2518/tests/checks/silent_defaults/test_ops.py
# Original detector semantics and fixtures retained; inventories use authored NMRNet paths.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..sources import ROOT
from .test_shared_scanner import is_literal_make_origin_gate, line_findings, repo_files

MAKE_DEFAULT_ASSIGNMENT_RE = re.compile(r"^(?:export\s+|override\s+)?(?P<name>[A-Za-z0-9_./-]+)\s*\?=\s*(?P<value>.*)$")
MAKE_FUNCTION_DEFAULT_RE = re.compile(r"\$\((?:or|if)\b[^)]*\)")
SHELL_DEFAULT_EXPANSION_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[#$!?*@_-])(?P<operator>:?[-=])(?P<value>[^}]*)\}"
)
SHELL_SILENT_SUCCESS_RE = re.compile(r"\|\|\s*(?:true|:)\b")
DOCKER_ARG_DEFAULT_RE = re.compile(r"^\s*ARG\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:=(?P<value>.*))?\s*$")
DOCKER_ENV_ASSIGNMENT_RE = re.compile(r"^\s*ENV\s+(?P<body>.+)$")
DOCKER_KEY_VALUE_ENV_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?P<value>.*)$")
DOCKER_LEGACY_ENV_TOKEN_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<value>.+)$")
ENV_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$")
YAML_ENV_LIST_ASSIGNMENT_RE = re.compile(r"^\s*-\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
YAML_ENV_MAPPING_BLANK_RE = re.compile(r"^\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?$")
YAML_ENVIRONMENT_BLOCK_RE = re.compile(r"^(?P<indent>\s*)environment:\s*$")
WORKFLOW_DEFAULT_FIELD_RE = re.compile(r"^\s*default:\s*(?P<value>.*)$")
ALLOWED_MAKE_DEFAULTS = {}
# The installed runtime supplies its operational guard marker and forbids
# bytecode writes on its read-only filesystem. Neither selects operator input.
ALLOWED_DOCKER_DEFAULTS = {
    ("ENV", "containers/delivery/Dockerfile", "PROVIDER_CORE_IN_CONTAINER", "1"),
    ("ENV", "containers/delivery/Dockerfile", "PYTHONDONTWRITEBYTECODE", "1"),
}
ALLOWED_SHELL_DEFAULTS = set()


def ops_files() -> list[Path]:
    return repo_files(
        (
            "Makefile",
            "*.mk",
            "compose/**/*.yml",
            "compose/**/*.yaml",
            "containers/**/Dockerfile*",
            "containers/**/*.sh",
            "tools/**/*.sh",
            "tools/**/*.py",
            "tests/**/*.sh",
            ".github/workflows/*.yml",
            ".github/workflows/*.yaml",
            "*.env",
            "*.env.example",
            "**/*.env",
            "**/*.env.example",
        )
    )


def shell_exported_names(source: str) -> set[str] | None:
    """Find literal publication candidates, or retain raw coverage when uncertain."""
    # The pinned Dapp detector matches assignments, not shell publication. In
    # tools/docker_client.sh, directory resets are private lifecycle state;
    # tools/inference_inputs.sh explicitly exports its subprocess environment.
    # Collect names across the source: even a blank corrected before export is
    # still flagged. This is conservative static policy, not execution tracing.
    # Dynamic exports, options and other publication forms retain raw findings.
    # Inherited export state and command indirection are outside this lexical
    # source check; it does not establish a process's complete environment.
    names: set[str] = set()
    for line in source.splitlines():
        if line.lstrip().startswith("#") or not re.search(r"\b(?:export|declare|typeset|set|shopt)\b", line):
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return None
        if "declare" in tokens or "typeset" in tokens:
            return None
        for builtin in ("set", "shopt"):
            if builtin not in tokens:
                continue
            if tokens.count(builtin) != 1:
                return None
            for token in tokens[tokens.index(builtin) + 1 :]:
                if not token:
                    return None
                if token[0] in ";&|()":
                    break
                if (
                    (builtin == "set" and re.fullmatch(r"[+-][A-Za-z]*a[A-Za-z]*", token))
                    or token == "allexport"
                    or "$" in token
                    or "`" in token
                ):
                    return None
        if "export" not in tokens:
            continue
        if tokens[0] != "export" or len(tokens) == 1:
            return None
        for token in tokens[1:]:
            name = token.partition("=")[0]
            match = ENV_ASSIGNMENT_RE.fullmatch(name + "=")
            if match is None or match.group("name") != name:
                return None
            names.add(name)
    return names


class OpsSilentDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_matchers_flag_representative_ops_defaults(self) -> None:
        self.assertRegex("PROFILE ?= demo", MAKE_DEFAULT_ASSIGNMENT_RE)
        self.assertRegex("$(or $(PROFILE),demo)", MAKE_FUNCTION_DEFAULT_RE)
        self.assertRegex("${PROFILE:-demo}", SHELL_DEFAULT_EXPANSION_RE)
        self.assertRegex("make clean || true", SHELL_SILENT_SUCCESS_RE)
        self.assertRegex("ARG SOLC_VERSION=0.8.35", DOCKER_ARG_DEFAULT_RE)
        self.assertRegex("TOKEN=", DOCKER_KEY_VALUE_ENV_TOKEN_RE)
        self.assertRegex("ENV XDG_DATA_HOME /usr/local/share", DOCKER_ENV_ASSIGNMENT_RE)
        self.assertRegex("XDG_DATA_HOME /usr/local/share", DOCKER_LEGACY_ENV_TOKEN_RE)
        self.assertRegex("FOO=", ENV_ASSIGNMENT_RE)
        self.assertRegex("      - FOO=", YAML_ENV_LIST_ASSIGNMENT_RE)
        self.assertRegex("    environment:", YAML_ENVIRONMENT_BLOCK_RE)
        self.assertRegex("      FOO:", YAML_ENV_MAPPING_BLANK_RE)
        self.assertEqual(
            ["Makefile:1: unreviewed Make default assignment: PROFILE ?= demo"],
            make_default_assignment_findings_for_source("PROFILE ?= demo\n", "Makefile"),
        )
        # The original fixture relies on its repository allowance. Keep that
        # positive/negative classification proof local to the detector fixture;
        # it grants no default in the source inventory used by repository scans.
        with patch.dict(ALLOWED_MAKE_DEFAULTS, {"ANVIL_HOST_PORT": "8545"}, clear=True):
            self.assertEqual(
                ["Makefile:1: changed Make default assignment: ANVIL_HOST_PORT ?= 9545"],
                make_default_assignment_findings_for_source("ANVIL_HOST_PORT ?= 9545\n", "Makefile"),
            )
        self.assertEqual(
            ["Dockerfile:1: changed Docker ARG default: ARG SOLC_VERSION=0.8.34"],
            docker_default_findings_for_source(
                "ARG SOLC_VERSION=0.8.34\n",
                "Dockerfile",
                {("ARG", "Dockerfile", "SOLC_VERSION", "0.8.35")},
            ),
        )
        self.assertEqual(
            ["Dockerfile:1: unreviewed Docker ENV default: ENV CACHE_DIR=/tmp/cache"],
            docker_default_findings_for_source(
                "ENV CACHE_DIR=/tmp/cache\n",
                "Dockerfile",
                {("ENV", "Dockerfile", "XDG_DATA_HOME", "/usr/local/share")},
            ),
        )

    def test_docker_continuations_preserve_every_default_and_start_line(self):
        source = "# fixture\nENV FIRST=one \\\n    SECOND=two \\\n    THIRD=three\n"
        self.assertEqual(
            [
                "Dockerfile:2: unreviewed Docker ENV default: ENV FIRST=one      SECOND=two      THIRD=three",
                "Dockerfile:2: unreviewed Docker ENV default: ENV FIRST=one      SECOND=two      THIRD=three",
                "Dockerfile:2: unreviewed Docker ENV default: ENV FIRST=one      SECOND=two      THIRD=three",
            ],
            docker_default_findings_for_source(source, "Dockerfile", set()),
        )
        self.assertEqual([(2, "ENV FIRST=one      SECOND=two      THIRD=three")], list(docker_logical_lines(source))[1:])
        continued = "ENV FIRST=one \\\n# Docker continuation comment\n\n    SECOND=two\n"
        self.assertEqual(
            [
                "Dockerfile:1: unreviewed Docker ENV default: ENV FIRST=one      SECOND=two",
                "Dockerfile:1: unreviewed Docker ENV default: ENV FIRST=one      SECOND=two",
            ],
            docker_default_findings_for_source(continued, "Dockerfile", set()),
        )
        self.assertEqual([(1, "ENV FIRST=one      SECOND=two")], list(docker_logical_lines(continued)))

    def test_ops_files_do_not_publish_silent_defaults(self) -> None:
        files = ops_files()
        findings: list[str] = []
        findings.extend(make_default_assignment_findings(files))
        findings.extend(docker_default_findings(files))
        findings.extend(make_function_default_findings(files))
        findings.extend(shell_default_findings(files))
        findings.extend(self.shell_silent_success_findings(files))
        findings.extend(line_findings(files, WORKFLOW_DEFAULT_FIELD_RE, "workflow dispatch default"))

        for path in files:
            source = path.read_text(encoding="utf-8")
            exported_names = shell_exported_names(source) if path.suffix == ".sh" else None
            environment_indent: int | None = None
            for line_number, line in enumerate(source.splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                block_match = YAML_ENVIRONMENT_BLOCK_RE.match(line)
                if block_match:
                    environment_indent = len(block_match.group("indent"))
                    continue
                if environment_indent is not None and line.strip():
                    current_indent = len(line) - len(line.lstrip())
                    if current_indent <= environment_indent:
                        environment_indent = None
                assignment_match = ENV_ASSIGNMENT_RE.match(line.strip())
                if (
                    assignment_match
                    and assignment_match.group("value").strip() in {"", '""', "''"}
                    and (exported_names is None or assignment_match.group("name") in exported_names)
                ):
                    findings.append(
                        f"{path.relative_to(ROOT)}:{line_number}: blank env assignment: "
                        f"{assignment_match.group('name')}={assignment_match.group('value')}"
                    )
                yaml_list_match = YAML_ENV_LIST_ASSIGNMENT_RE.match(line)
                if yaml_list_match and yaml_list_match.group("value").strip() in {"", '""', "''"}:
                    findings.append(
                        f"{path.relative_to(ROOT)}:{line_number}: blank compose env assignment: {yaml_list_match.group('name')}="
                    )
                yaml_mapping_match = YAML_ENV_MAPPING_BLANK_RE.match(line) if environment_indent is not None else None
                if yaml_mapping_match:
                    findings.append(
                        f"{path.relative_to(ROOT)}:{line_number}: blank compose env mapping: {yaml_mapping_match.group('name')}:"
                    )

        self.assertEqual([], findings)

    def test_shell_publication_names_are_conservative_source_candidates(self):
        cases = (
            ("directory=\ndirectory=ready\ndirectory=\n", set()),
            ("export TOKEN\nTOKEN=\n", {"TOKEN"}),
            ("TOKEN=\nexport TOKEN\n", {"TOKEN"}),
            ("TOKEN=\nTOKEN=ready\nexport TOKEN\n", {"TOKEN"}),
            ("export 'TOKEN' \"OTHER=value\" THIRD=ready\n", {"TOKEN", "OTHER", "THIRD"}),
            ('export "$name"\nTOKEN=\n', None),
            ("export 'export TOKEN'\nTOKEN=\n", None),
            ("export -n TOKEN\nTOKEN=\n", None),
            ("declare -x TOKEN\nTOKEN=\n", None),
            ("typeset -x TOKEN\nTOKEN=\n", None),
            ("set '-a'\nTOKEN=\n", None),
            ("set -o allexport\nTOKEN=\n", None),
            ('set "$flags"\nTOKEN=\n', None),
            ('set -m; set "$flags"\nTOKEN=\n', None),
            ("shopt -s -o allexport\nTOKEN=\n", None),
            ("export TOKEN\nshopt -s nullglob dotglob\n", {"TOKEN"}),
            ("TOKEN=; export TOKEN\n", None),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, shell_exported_names(source))

    def test_ops_property_distinguishes_private_resets_from_blank_publication(self):
        cases = (
            ("fixture.sh", "directory=\ndirectory=ready\ndirectory=\n", None),
            ("fixture.sh", "export TOKEN\nTOKEN=\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\nexport TOKEN\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\nTOKEN=ready\nexport TOKEN\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\nexport 'TOKEN' OTHER=ready\n", "blank env assignment: TOKEN="),
            ("fixture.sh", 'TOKEN=\nexport "$name"\n', "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\nexport 'export TOKEN'\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\nexport -n TOKEN\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\ndeclare -x TOKEN\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\ntypeset -x TOKEN\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\nset '-a'\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\nset -o allexport\n", "blank env assignment: TOKEN="),
            ("fixture.sh", 'TOKEN=\nset "$flags"\n', "blank env assignment: TOKEN="),
            ("fixture.sh", 'TOKEN=\nset -m; set "$flags"\n', "blank env assignment: TOKEN="),
            ("fixture.sh", "TOKEN=\nshopt -s -o allexport\n", "blank env assignment: TOKEN="),
            ("fixture.sh", "directory=\nshopt -s nullglob dotglob\n", None),
            ("fixture.env", "TOKEN=\n", "blank env assignment: TOKEN="),
            ("fixture.env.example", "TOKEN=''\n", "blank env assignment: TOKEN=''"),
            ("compose.yml", "environment:\n  TOKEN:\n", "blank compose env mapping: TOKEN:"),
            ("compose.yml", "environment:\n  - TOKEN=\n", "blank compose env assignment: TOKEN="),
            ("workflow.yml", "  default: ready\n", "workflow dispatch default"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, source, diagnostic in cases:
                with self.subTest(label=label, source=source):
                    path = root / label
                    path.write_text(source, encoding="utf-8")
                    with (
                        patch(__name__ + ".ROOT", root),
                        patch(__package__ + ".test_shared_scanner.ROOT", root),
                        patch(__name__ + ".ops_files", return_value=[path]),
                    ):
                        if diagnostic is None:
                            self.test_ops_files_do_not_publish_silent_defaults()
                        else:
                            with self.assertRaises(AssertionError) as failure:
                                self.test_ops_files_do_not_publish_silent_defaults()
                            self.assertIn(diagnostic, str(failure.exception))

    def test_allowed_make_defaults_match_exact_values(self) -> None:
        defaults = make_defaults(ROOT / "Makefile")

        self.assertEqual(ALLOWED_MAKE_DEFAULTS, defaults)

    def test_allowed_docker_defaults_match_exact_values(self) -> None:
        defaults = docker_defaults(ops_files())

        self.assertEqual(ALLOWED_DOCKER_DEFAULTS, defaults)

    def test_runtime_docker_environment_contracts_reject_value_path_and_instruction_changes(self):
        """Reviewing an image ENV value grants no changed value, path or build ARG."""
        # Synthetic positive ownership keeps this detector exercised before
        # selecting a production value. It grants no additional image default.
        allowed = {("ENV", "containers/checker/Dockerfile", "MARKER", "1")}
        self.assertEqual([], docker_default_findings_for_source("ENV MARKER=1\n", "containers/checker/Dockerfile", allowed))
        changes = (
            ("ENV MARKER=changed-1", "containers/checker/Dockerfile", "changed", "ENV"),
            ("ENV MARKER=1", "containers/unreviewed/Dockerfile", "unreviewed", "ENV"),
            ("ARG MARKER=1", "containers/checker/Dockerfile", "unreviewed", "ARG"),
        )
        for source, path, classification, instruction in changes:
            with self.subTest(source=source, path=path):
                self.assertEqual(
                    [f"{path}:1: {classification} Docker {instruction} default: {source}"],
                    docker_default_findings_for_source(source + "\n", path, allowed),
                )

    def test_allowed_shell_defaults_are_present_with_exact_values(self) -> None:
        defaults = shell_defaults(ops_files())

        self.assertEqual(ALLOWED_SHELL_DEFAULTS, defaults)

    def shell_silent_success_findings(self, files: list[Path]) -> list[str]:
        findings: list[str] = []
        for path in files:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if not SHELL_SILENT_SUCCESS_RE.search(line):
                    continue
                findings.append(f"{path.relative_to(ROOT)}:{line_number}: shell silent success fallback: {line.strip()}")
        return findings


def make_function_default_findings(files: list[Path]) -> list[str]:
    """Retain raw Make fallback matches except complete literal origin gates."""
    findings = []
    for path in files:
        source = path.relative_to(ROOT).as_posix()
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#") or is_literal_make_origin_gate(source, line):
                continue
            for match in MAKE_FUNCTION_DEFAULT_RE.finditer(line):
                findings.append(f"{source}:{line_number}: make function fallback: {match.group(0).strip()}")
    return findings


def make_default_assignment_findings(files: list[Path]) -> list[str]:
    findings: list[str] = []
    for path in files:
        findings.extend(
            make_default_assignment_findings_for_source(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
        )
    return findings


def make_default_assignment_findings_for_source(source: str, label: str) -> list[str]:
    findings: list[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        match = MAKE_DEFAULT_ASSIGNMENT_RE.match(line)
        if match is None:
            continue
        name = match.group("name")
        value = match.group("value")
        expected = ALLOWED_MAKE_DEFAULTS.get(name)
        if expected is None:
            findings.append(f"{label}:{line_number}: unreviewed Make default assignment: {line.strip()}")
        elif expected != value:
            findings.append(f"{label}:{line_number}: changed Make default assignment: {line.strip()}")
    return findings


def docker_default_findings(files: list[Path]) -> list[str]:
    findings: list[str] = []
    for path in files:
        label = path.relative_to(ROOT).as_posix()
        findings.extend(docker_default_findings_for_source(path.read_text(encoding="utf-8"), label, ALLOWED_DOCKER_DEFAULTS))
    return findings


def docker_default_findings_for_source(
    source: str,
    label: str,
    allowed: set[tuple[str, str, str, str]],
) -> list[str]:
    findings: list[str] = []
    for line_number, line in docker_logical_lines(source):
        arg_match = DOCKER_ARG_DEFAULT_RE.match(line)
        if arg_match and arg_match.group("value") is not None:
            name = arg_match.group("name")
            value = arg_match.group("value")
            entry = ("ARG", label, name, value)
            if entry not in allowed:
                classification = docker_default_classification(entry, allowed)
                findings.append(f"{label}:{line_number}: {classification} Docker ARG default: {line.strip()}")
            continue

        env_match = DOCKER_ENV_ASSIGNMENT_RE.match(line)
        if env_match is None:
            continue
        body = env_match.group("body")
        tokens = shlex.split(body)
        if not tokens:
            continue
        if all(DOCKER_KEY_VALUE_ENV_TOKEN_RE.match(token) for token in tokens):
            for token in tokens:
                name, value = token.split("=", 1)
                entry = ("ENV", label, name, value)
                if entry not in allowed:
                    classification = docker_default_classification(entry, allowed)
                    findings.append(f"{label}:{line_number}: {classification} Docker ENV default: {line.strip()}")
            continue
        legacy_match = DOCKER_LEGACY_ENV_TOKEN_RE.match(body)
        if legacy_match is None:
            findings.append(f"{label}:{line_number}: unparseable Docker ENV default: {line.strip()}")
            continue
        name = legacy_match.group("name")
        value = legacy_match.group("value")
        entry = ("ENV", label, name, value)
        if entry not in allowed:
            classification = docker_default_classification(entry, allowed)
            findings.append(f"{label}:{line_number}: {classification} Docker ENV default: {line.strip()}")

    return findings


def docker_logical_lines(source):
    """Join Docker instruction continuations without losing their first line.

    The source scanner handled one-line ENV forms. NMRNet's authored images use
    multiline ENV, so tokenizing each physical line would crash at a valid final
    backslash and leave its later defaults unchecked.
    """
    pending = ""
    first_line = 1
    for line_number, line in enumerate(source.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            if pending:
                continue
            yield line_number, line
            continue
        if not pending:
            first_line = line_number
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        yield first_line, pending + line
        pending = ""
    if pending:
        # A trailing continuation is malformed, not an empty/default waiver.
        yield first_line, pending + "\\"


def docker_default_classification(
    entry: tuple[str, str, str, str],
    allowed: set[tuple[str, str, str, str]],
) -> str:
    return "changed" if any(allowed_entry[:3] == entry[:3] for allowed_entry in allowed) else "unreviewed"


def shell_default_findings(files: list[Path]) -> list[str]:
    findings: list[str] = []
    defaults = shell_defaults(files)
    for entry in sorted(defaults):
        if entry not in ALLOWED_SHELL_DEFAULTS:
            path, name, operator, value = entry
            findings.append(f"{path}: unreviewed shell default expansion: ${{{name}{operator}{value}}}")
    return findings


def make_defaults(path: Path) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = MAKE_DEFAULT_ASSIGNMENT_RE.match(line)
        if match:
            defaults[match.group("name")] = match.group("value")
    return defaults


def docker_defaults(files: list[Path]) -> set[tuple[str, str, str, str]]:
    defaults: set[tuple[str, str, str, str]] = set()
    for path in files:
        label = path.relative_to(ROOT).as_posix()
        for _line_number, line in docker_logical_lines(path.read_text(encoding="utf-8")):
            arg_match = DOCKER_ARG_DEFAULT_RE.match(line)
            if arg_match and arg_match.group("value") is not None:
                defaults.add(("ARG", label, arg_match.group("name"), arg_match.group("value")))
                continue

            env_match = DOCKER_ENV_ASSIGNMENT_RE.match(line)
            if env_match is None:
                continue
            body = env_match.group("body")
            tokens = shlex.split(body)
            if tokens and all(DOCKER_KEY_VALUE_ENV_TOKEN_RE.match(token) for token in tokens):
                for token in tokens:
                    name, value = token.split("=", 1)
                    defaults.add(("ENV", label, name, value))
                continue
            legacy_match = DOCKER_LEGACY_ENV_TOKEN_RE.match(body)
            if legacy_match:
                defaults.add(("ENV", label, legacy_match.group("name"), legacy_match.group("value")))
    return defaults


def shell_defaults(files: list[Path]) -> set[tuple[str, str, str, str]]:
    defaults: set[tuple[str, str, str, str]] = set()
    for path in files:
        label = path.relative_to(ROOT).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in SHELL_DEFAULT_EXPANSION_RE.finditer(line):
                defaults.add((label, match.group("name"), match.group("operator"), match.group("value")))
    return defaults
