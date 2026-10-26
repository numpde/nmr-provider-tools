"""Keep operator discovery and Make target ownership complete and explicit.

Source groups are copied from the pinned hygiene suite; only the local public
aggregate names and authored Python roots differ. Dapp release, Foundry, CAM,
branding and license-marker assertions have no corresponding owner here.
"""

from tools.container_guard import require_container

require_container()

# Port source: https://github.com/numpde/dapp32/blob/778c16f8d8b77181d2ac4cd11a62a2034ded2518/tests/checks/test_repository_hygiene.py
# Original groups: Python bytecode, first-party symlinks, help completeness,
# scoped targets, aggregate campaigns, sole entrypoint, phony/default/duplicates.
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .sources import is_skipped, iter_repo_text_files, read_text, repo_path

MAKE_TARGET_RE = re.compile(r"^(?P<names>[A-Za-z0-9_/-]+(?:[ \t]+[A-Za-z0-9_/-]+)*)[ \t]*:(?!:*=)[ \t]*(?P<prereqs>.*)$")
MAKE_HELP_TARGET_RE = re.compile(r"\bmake\s+(?P<name>[A-Za-z0-9_/-]+)\b")
MAKE_PHONY_RE = re.compile(r"^\.PHONY:\s*(?P<names>.*)$", re.MULTILINE)
# Dapp requires explicit safe discovery; API additionally requires caller-proof
# ownership. Accept that modifier without admitting a second default authority.
MAKE_DEFAULT_GOAL_RE = re.compile(
    r"^(?:override[ \t]+)?\.DEFAULT_GOAL[ \t]*:?=[ \t]*(?P<name>[A-Za-z0-9_/-]+)[ \t]*$", re.MULTILINE
)
MAKE_DEFAULT_AUTHORITY_RE = re.compile(
    r"^[ \t]*(?:(?:override|export|private)[ \t]+)*\.DEFAULT_GOAL[ \t]*(?:::?=|[?+!]?=)", re.MULTILINE
)
FIRST_PARTY_PYTHON_ROOTS = (
    "tools",
    "tests",
    "containers",
    "src",
    "packages",
)
SECRET_PATTERNS = [
    re.compile("GITHUB_" + "ACCESS_TOKEN"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
]


class RepositoryHygieneTest(unittest.TestCase):
    def test_secret_text_patterns_are_absent(self):
        findings = []
        for path in iter_repo_text_files():
            for line_number, line in enumerate(read_text(path).splitlines(), start=1):
                if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                    findings.append(f"{path}:{line_number}: secret pattern")
        self.assertEqual([], findings, msg="\n".join(findings))

    def test_first_party_python_bytecode_is_not_materialized_on_host(self) -> None:
        failures: list[str] = []

        # Supported Python lanes disable bytecode writes. If first-party Python
        # leaves pyc artifacts on the host, someone bypassed that boundary or a
        # lane regressed.
        for root_name in FIRST_PARTY_PYTHON_ROOTS:
            root = repo_path(root_name)
            for path in root.rglob("*"):
                if path.is_dir() and path.name == "__pycache__":
                    failures.append(f"{path}: Python bytecode cache must not be materialized on host")
                elif path.is_file() and path.suffix == ".pyc":
                    failures.append(f"{path}: Python bytecode must not be materialized on host")

        if failures:
            self.fail("\n".join(failures))

    def test_first_party_paths_do_not_use_symlinks(self) -> None:
        failures: list[str] = []

        # Docker bind mounts, staged workspaces, and CAM publication tools all
        # reason about repository paths before giving any lane write authority.
        # Keep first-party paths real; dependency/install trees are separate
        # materialized outputs with their own integrity checks.
        for path in repo_path(".").rglob("*"):
            if is_skipped(path):
                continue
            if path.is_symlink():
                failures.append(f"{path}: first-party repository paths must not be symlinks")

        if failures:
            self.fail("\n".join(failures))

    def test_make_help_mentions_existing_targets(self) -> None:
        makefile = read_text(repo_path("Makefile"))
        targets = self.make_targets(makefile)
        advertised = set(MAKE_HELP_TARGET_RE.findall(self.make_help_recipe(makefile)))

        # `make help` is the operator's map of supported entrypoints. If it
        # advertises a stale target, the safest path becomes guesswork.
        self.assertEqual(targets, advertised)

    def test_make_scoped_targets_are_hierarchical(self) -> None:
        targets = self.make_targets(read_text(repo_path("Makefile")))
        for target in targets - {"help", "tests"}:
            with self.subTest(target=target):
                self.assertRegex(target, r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+$")

    def test_make_test_scopes_run_their_supported_campaigns(self) -> None:
        # The public aggregate covers repository checks and every installed-core lane.
        makefile = read_text(repo_path("Makefile"))
        self.assertEqual(
            ("tests/repository", "tests/unit/provider-core", "tests/contract/provider-core", "tests/integration/provider-core"),
            self.make_target_prereqs(makefile, "tests"),
        )
        self.assertEqual("", self.make_target_recipe(makefile, "tests").strip())

    def test_repository_has_one_make_entrypoint(self) -> None:
        make_entrypoints = [name for name in ["GNUmakefile", "makefile", "Makefile"] if repo_path(name).exists()]

        # GNU Make silently prefers GNUmakefile, then makefile, over Makefile.
        # A second root entrypoint can therefore bypass the operator contract
        # and the checks that intentionally inspect Makefile.
        self.assertEqual(["Makefile"], make_entrypoints)

    def test_make_phony_targets_are_real_targets(self) -> None:
        makefile = read_text(repo_path("Makefile"))
        targets = self.make_targets(makefile)
        phony_targets = {name for match in MAKE_PHONY_RE.finditer(makefile) for name in match.group("names").split()}

        # Phony declarations are also operator API inventory. A stale `.PHONY`
        # entry makes it harder to review which Make names are real lanes.
        self.assertEqual(targets, phony_targets)

    def test_make_default_goal_is_explicit_help_target(self) -> None:
        makefile = read_text(repo_path("Makefile"))
        self.assertEqual(
            1, len(MAKE_DEFAULT_AUTHORITY_RE.findall(makefile)), msg="Makefile must have exactly one default-goal assignment"
        )
        match = MAKE_DEFAULT_GOAL_RE.search(makefile)
        if match is None:
            self.fail("Makefile must declare an explicit default goal")

        goal = match.group("name")
        # The default invocation should be safe discovery, not an execution
        # lane. Keep `make` equivalent to `make help`.
        self.assertEqual("help", goal)
        self.assertIn(goal, self.make_targets(makefile))

    def test_default_goal_accepts_one_bare_or_owned_help_assignment(self):
        for assignment in (".DEFAULT_GOAL = help", ".DEFAULT_GOAL := help", "override .DEFAULT_GOAL := help"):
            with self.subTest(assignment=assignment), patch(f"{__name__}.read_text", return_value=assignment + "\nhelp:\n"):
                self.test_make_default_goal_is_explicit_help_target()

    def test_default_goal_rejects_changed_missing_and_duplicate_authority(self):
        for assignments in (
            "",
            ".DEFAULT_GOAL := weights/download",
            "override .DEFAULT_GOAL := test",
            ".DEFAULT_GOAL := help\noverride .DEFAULT_GOAL := help",
            "override .DEFAULT_GOAL := help\n override .DEFAULT_GOAL := test",
            "override .DEFAULT_GOAL := help\n.DEFAULT_GOAL := $(shell private-effect)",
            "override .DEFAULT_GOAL := help\nexport .DEFAULT_GOAL = test",
        ):
            with (
                self.subTest(assignments=assignments),
                patch(f"{__name__}.read_text", return_value=assignments + "\nhelp:\n"),
                self.assertRaises(AssertionError),
            ):
                self.test_make_default_goal_is_explicit_help_target()

    def test_make_targets_are_unique(self) -> None:
        makefile = read_text(repo_path("Makefile"))
        targets = self.make_target_list(makefile)
        duplicates = sorted({target for target in targets if targets.count(target) > 1})

        # GNU Make allows later recipes to override earlier ones. That is too
        # implicit for operator lanes; duplicate targets should be reviewed as
        # an intentional refactor instead.
        self.assertEqual([], duplicates)

    def test_multi_target_rules_preserve_each_names_prerequisites_and_recipe(self):
        makefile = (
            "first/one second/two: common/input # shared prerequisite\n"
            "\t@echo $@\n"
            "common/input:\n"
            "\t@echo input\n"
            "next/three:\n"
            "\t@echo next\n"
        )
        self.assertEqual(["first/one", "second/two", "common/input", "next/three"], self.make_target_list(makefile))
        for target in ("first/one", "second/two"):
            with self.subTest(target=target):
                self.assertEqual(("common/input",), self.make_target_prereqs(makefile, target))
                self.assertEqual("\t@echo $@", self.make_target_recipe(makefile, target))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Makefile"
            path.write_text(makefile)
            result = subprocess.run(
                ["make", "--no-print-directory", "--dry-run", "-f", str(path), "first/one", "second/two"],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(["echo input", "echo first/one", "echo second/two"], result.stdout.splitlines())

    def test_target_parser_excludes_assignments_and_recipe_text(self):
        makefile = (
            "ROOT := /workspace\nROOT ::= /workspace\nROOT :::= /workspace\n"
            "ROOT = value\nROOT += value\nROOT ?= value\nROOT != printf value\n"
            ".PHONY: help real/one real/two\n"
            "help:\n"
            "\t@echo 'recipe/target: hidden'\n"
            "real/one real/two:\n"
            "\t@echo $@\n"
        )
        self.assertEqual(["help", "real/one", "real/two"], self.make_target_list(makefile))
        self.assertEqual("\t@echo 'recipe/target: hidden'", self.make_help_recipe(makefile))

    def test_duplicate_detection_retains_names_repeated_across_multi_target_rules(self):
        makefile = "first/one second/two:\nsecond/two third/three:\n"
        self.assertEqual(["first/one", "second/two", "second/two", "third/three"], self.make_target_list(makefile))
        with patch(f"{__name__}.read_text", return_value=makefile), self.assertRaises(AssertionError):
            self.test_make_targets_are_unique()

    def test_multi_target_inventory_still_rejects_stale_help_and_phony_declarations(self):
        makefile = (
            ".PHONY: help real/one real/two\n"
            "help:\n"
            "\t@echo 'make help'\n"
            "\t@echo 'make real/one'\n"
            "\t@echo 'make real/two'\n"
            "real/one real/two:\n"
            "\t@echo $@\n"
        )
        with patch(f"{__name__}.read_text", return_value=makefile):
            self.test_make_help_mentions_existing_targets()
            self.test_make_phony_targets_are_real_targets()
        with (
            patch(f"{__name__}.read_text", return_value=makefile.replace("help:\n", "help:\n\t@echo 'make stale/lane'\n")),
            self.assertRaises(AssertionError),
        ):
            self.test_make_help_mentions_existing_targets()
        with (
            patch(f"{__name__}.read_text", return_value=makefile + ".PHONY: stale/lane\n"),
            self.assertRaises(AssertionError),
        ):
            self.test_make_phony_targets_are_real_targets()

    def make_help_recipe(self, makefile: str) -> str:
        lines = makefile.splitlines()
        start = lines.index("help:") + 1
        recipe: list[str] = []
        for line in lines[start:]:
            if MAKE_TARGET_RE.match(line):
                break
            recipe.append(line)
        return "\n".join(recipe)

    def make_target_recipe(self, makefile: str, target: str) -> str:
        lines = makefile.splitlines()
        for index, line in enumerate(lines):
            match = MAKE_TARGET_RE.match(line)
            if match is not None and target in match.group("names").split():
                recipe: list[str] = []
                for recipe_line in lines[index + 1 :]:
                    if MAKE_TARGET_RE.match(recipe_line):
                        break
                    recipe.append(recipe_line)
                return "\n".join(recipe)
        raise self.failureException(f"missing Make target: {target}")

    def make_targets(self, makefile: str) -> set[str]:
        return set(self.make_target_list(makefile))

    def make_target_list(self, makefile: str) -> list[str]:
        return [
            name
            for line in makefile.splitlines()
            if (match := MAKE_TARGET_RE.match(line)) is not None
            for name in match.group("names").split()
        ]

    def make_target_prereqs(self, makefile: str, target: str) -> tuple[str, ...]:
        for line in makefile.splitlines():
            match = MAKE_TARGET_RE.match(line)
            if match is not None and target in match.group("names").split():
                return tuple(match.group("prereqs").split("#", 1)[0].split())
        raise self.failureException(f"missing Make target: {target}")
