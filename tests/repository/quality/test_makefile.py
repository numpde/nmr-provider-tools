"""Exact Make source inspection and command discovery, with original fixtures."""

from tools.container_guard import require_container

require_container()

# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/test_makefile.py
# All MakeSourceInspectionTests and MakeHelpTests retained. DeploymentDiagnostics
# owns API Compose topology, which is absent here. Recipe override ownership
# maps the source credential publication/default-goal probes to local targets.
import os
import re
import shutil
import subprocess
import unittest

from .makefile import make_define_body, make_invocation_owners, make_target_body
from .sources import REPOSITORY_ROOT


class MakeSourceInspectionTests(unittest.TestCase):
    def test_returns_one_repository_owned_define_body(self) -> None:
        makefile = "override define OWNER\nfirst\nsecond\nendef\n"

        self.assertEqual("first\nsecond", make_define_body(makefile, "OWNER"))

    def test_rejects_missing_duplicate_or_unowned_defines(self) -> None:
        for makefile, message in (
            ("define OWNER\nbody\nendef\n", "must appear once"),
            ("override define OWNER\nbody\n", "has no endef"),
            (
                "override define OWNER\nbody\nendefinitely\n",
                "has no endef",
            ),
            (
                "override define OWNER\none\nendef\noverride define OWNER\ntwo\nendef\n",
                "must appear once",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(AssertionError, message):
                make_define_body(makefile, "OWNER")

    def test_returns_only_the_selected_recipe(self) -> None:
        makefile = "first:\n\t@printf first\nsecond:\n\t@printf second\n"

        self.assertEqual(
            "\t@printf first\n",
            make_target_body(makefile, "first"),
        )

    def test_accepts_prerequisites_without_confusing_them_for_targets(self) -> None:
        for separator in (": ", ":"):
            with self.subTest(separator=separator):
                makefile = f"owner{separator}prerequisite\n\t@printf owner\n"
                self.assertEqual(
                    "\t@printf owner\n",
                    make_target_body(makefile, "owner"),
                )
                with self.assertRaisesRegex(
                    AssertionError,
                    "target 'prerequisite' is missing",
                ):
                    make_target_body(makefile, "prerequisite")

    def test_requires_an_exact_sole_literal_target(self) -> None:
        makefile = "wanted-extra:\n\t@printf wrong\n"

        with self.assertRaisesRegex(AssertionError, "target 'wanted' is missing"):
            make_target_body(makefile, "wanted")

    def test_rejects_ambiguous_or_recipe_less_rules(self) -> None:
        cases = (
            (
                "duplicate:\n\t@printf one\nduplicate:\n\t@printf two\n",
                "duplicate",
                "multiple literal rules",
            ),
            (
                "duplicate:\n\t@printf one\nduplicate:prerequisite\n\t@printf two\n",
                "duplicate",
                "multiple literal rules",
            ),
            ("alias: dependency\n", "alias", "has no recipe"),
            (
                "inline: ; @printf inline\n",
                "inline",
                "must have one plain literal rule",
            ),
            (
                "inline: ; @printf inline\n\t@printf another\n",
                "inline",
                "must have one plain literal rule",
            ),
            (
                "first second:\n\t@printf grouped\n",
                "first",
                "must have one plain literal rule",
            ),
            (
                "double::\n\t@printf double\n",
                "double",
                "must have one plain literal rule",
            ),
        )
        for makefile, target, message in cases:
            with self.subTest(target=target), self.assertRaisesRegex(AssertionError, message):
                make_target_body(makefile, target)

    def test_recipe_includes_lines_after_blank_lines_and_comments(self) -> None:
        makefile = "split:\n\t@printf first\n\n# explanation\n\t@printf second\n"
        self.assertEqual(
            "\t@printf first\n\n# explanation\n\t@printf second\n",
            make_target_body(makefile, "split"),
        )

    def test_invocation_inventory_names_every_direct_owner(self) -> None:
        makefile = "override define SHARED\n\t$(COMMAND)\nendef\nfirst:\n\t$(COMMAND)\nsecond:prerequisite\n\t$(COMMAND)\n"
        self.assertEqual(
            ["define SHARED", "target first", "target second"],
            make_invocation_owners(makefile, "$(COMMAND)"),
        )

    def test_invocation_inventory_rejects_uninspected_uses(self) -> None:
        for makefile in (
            "first:\n\t$(COMMAND)\nloose = $(COMMAND)\n",
            "first:\n\t$(COMMAND)\nUNRELATED = literal\n\t$(COMMAND)\n",
            "first:\n\t@# $(COMMAND)\n",
            "override define SHARED\ninner:\n\t$(COMMAND)\nendef\nloose = $(COMMAND)\n",
        ):
            with self.subTest(makefile=makefile), self.assertRaisesRegex(AssertionError, "outside one direct recipe"):
                make_invocation_owners(makefile, "$(COMMAND)")


class MakeHelpTests(unittest.TestCase):
    def test_literal_help_commands_name_make_targets(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        help_recipe = make_target_body(makefile, "help")
        literal_targets = set(re.findall(r"\bmake ([a-z0-9][a-z0-9/-]*)(?=\s|')", help_recipe))
        self.assertTrue(literal_targets)
        for target in sorted(literal_targets):
            with self.subTest(target=target):
                self.assertRegex(makefile, rf"(?m)^{re.escape(target)}:")


class MakeRecipeOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.make = shutil.which("make")
        if self.make is None:
            self.fail("make must be installed in the repository-check image")

    def test_recipe_macros_ignore_makeflags_assignments(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertNotRegex(makefile, r"(?m)^define ")
        for assignment in (
            "override SHELL := bash",
            "override .SHELLFLAGS := -eu -o pipefail -c",
            "override .DEFAULT_GOAL := help",
        ):
            self.assertIn(assignment, makefile)

        result = self._dry_run(
            "tests/repository",
            makeflags="SHELL=malicious",
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertNotIn("malicious", result.stdout)
        self.assertIn(
            "tools/repository.sh",
            result.stdout,
        )

        default_goal = self._dry_run(
            None,
            makeflags=".DEFAULT_GOAL=tests/repository",
        )
        self.assertEqual(0, default_goal.returncode, msg=default_goal.stderr)
        self.assertIn("make tests", default_goal.stdout)
        self.assertNotIn("bash tools/repository.sh tests/repository", default_goal.stdout)

    def _dry_run(
        self,
        target: str | None,
        *,
        makeflags: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{REPOSITORY_ROOT / 'tests/repository/fixtures/docker-context'}:{environment['PATH']}"
        if makeflags is not None:
            environment["MAKEFLAGS"] = makeflags
        command = [self.make, "--no-print-directory", "--dry-run"]
        if target is not None:
            command.extend((target, "DOCKER_CONTEXT=default"))
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
