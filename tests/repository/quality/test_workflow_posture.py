# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/test_workflow_posture.py
# Preserve workflow safety; local CI existence follows the reviewed inventory.
"""Repository checks for GitHub workflow source posture.

These checks cover workflow-wide source rules that rendered Compose config
cannot prove. Product behavior and test-lane semantics stay in their
boundary-specific tests.
"""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .sources import REPOSITORY_ROOT

WORKFLOW_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
# This port does not introduce a hosted CI workflow (notes/001). Adding a workflow requires review of
# this inventory; discovery alone cannot authorize a new execution surface.
REVIEWED_WORKFLOWS: tuple[str, ...] = ()
ACTION_USES_LINE = re.compile(r"(?m)^[ \t]+uses:[ \t]+(?P<target>\S+)")
PINNED_EXTERNAL_USES = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def workflow_files() -> list[Path]:
    return sorted(
        [
            *WORKFLOW_ROOT.glob("*.yml"),
            *WORKFLOW_ROOT.glob("*.yaml"),
        ]
    )


def read_workflow(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def unpinned_external_uses(workflow: str) -> list[str]:
    offenders: list[str] = []
    for match in ACTION_USES_LINE.finditer(workflow):
        target = match.group("target")
        if target.startswith("./"):
            continue
        if PINNED_EXTERNAL_USES.fullmatch(target) is None:
            offenders.append(match.group(0).strip())
    return offenders


class WorkflowPostureTests(unittest.TestCase):
    maxDiff = None

    def test_workflow_inventory_matches_reviewed_ci_scope(self) -> None:
        self.assertEqual(
            sorted(REVIEWED_WORKFLOWS),
            [path.name for path in workflow_files()],
            msg="workflow inventory differs from the reviewed CI scope",
        )

    def test_unreviewed_workflow_cannot_expand_deferred_ci_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unreviewed.yml").write_text("name: unreviewed\n", encoding="utf-8")
            with patch(f"{__name__}.WORKFLOW_ROOT", root), self.assertRaises(AssertionError):
                self.test_workflow_inventory_matches_reviewed_ci_scope()

    def test_external_workflow_actions_are_commit_pinned(self) -> None:
        for path in workflow_files():
            workflow = read_workflow(path)
            with self.subTest(workflow=path.name):
                self.assertEqual([], unpinned_external_uses(workflow))

    def test_workflows_do_not_define_silent_dispatch_defaults(self) -> None:
        for path in workflow_files():
            workflow = read_workflow(path)
            with self.subTest(workflow=path.name):
                self.assertNotRegex(workflow, r"(?m)^\s+default:\s*")

    def test_workflows_do_not_define_blank_environment_entries(self) -> None:
        for path in workflow_files():
            workflow = read_workflow(path)
            with self.subTest(workflow=path.name):
                self.assertNotRegex(workflow, r"(?m)^\s+[A-Z][A-Z0-9_]*:\s*$")

    def test_workflows_pin_github_hosted_runner_images(self) -> None:
        for path in workflow_files():
            workflow = read_workflow(path)
            with self.subTest(workflow=path.name):
                self.assertNotIn("ubuntu-latest", workflow)
                self.assertRegex(workflow, r"(?m)^\s+runs-on:\s+ubuntu-24\.04$")


if __name__ == "__main__":
    unittest.main()
