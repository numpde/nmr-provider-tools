"""Preserve canonical guard and module import ordering.

Source: secs-repro 34f5915c1534278bbf61b4c6592afa44cf94ab08,
secs/.github/workflows/ci.yml, nested secs 0414b5f9d65e61a6ba00aa046e24b8e48ce230b1.
Ruff execution and formatting belong to the separate ruffer repository.
The independent guard/import-order detectors remain local.
https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/secs/.github/workflows/ci.yml
"""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from tests.repository.quality.sources import ROOT, source_files
from tests.repository.test_conventions import has_early_guard


def late_module_imports(source, *, package=None):
    """Allow the required early guard, then retain module import ordering.

    Only a canonical early guard may precede the import section. Later effects
    cannot reopen it. This local detector remains independent of the external
    Ruffer formatting workflow.
    """
    statements = ast.parse(source).body
    guarded = has_early_guard(source, package=package) or has_early_guard(source, guard_module="provider_core.container_guard", package=package)
    imports_ended = False
    findings = []

    for (index, statement) in enumerate(statements):
        if (
            (index == 0) and
            isinstance(statement, ast.Expr) and
            isinstance(statement.value, ast.Constant) and
            isinstance(statement.value.value, str)
        ):
            continue

        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            if imports_ended:
                findings.append(statement.lineno)

            continue

        if (
            (guarded) and
            isinstance(statement, ast.Expr) and
            isinstance(statement.value, ast.Call) and
            isinstance(statement.value.func, ast.Name) and
            (statement.value.func.id == "require_container") and
            (not imports_ended)
        ):
            guarded = False

            continue

        imports_ended = True

    return findings


class ImportOrderDetectionTests(unittest.TestCase):
    def test_only_canonical_early_guard_may_precede_import_section(self):
        guard = (
            "from tools.container_guard import require_container\n"
            "require_container()\n"
        )

        self.assertEqual(
            [],
            late_module_imports(
                '"""Owner."""\n' + guard + (
                    "import pickle\n"
                    "VALUE = 1\n"
                )
            ),
        )

        self.assertEqual(
            [4],
            late_module_imports(
                guard + (
                    "VALUE = 1\n"
                    "import pickle\n"
                )
            ),
        )

        self.assertEqual([2, 4], late_module_imports("work()\n" + guard + "import pickle\n"))

        self.assertEqual(
            [3],
            late_module_imports(
                "import os\n"
                "work()\n"
                "import pickle\n"
            ),
        )

        self.assertEqual(
            [2],
            late_module_imports(
                "require_container()\n"
                "import pickle\n"
            ),
        )

        self.assertEqual(
            [4],
            late_module_imports(
                guard + (
                    "require_container()\n"
                    "import pickle\n"
                )
            ),
        )

        core_guard = (
            "from provider_core.container_guard import require_container\n"
            "require_container()\n"
        )

        self.assertEqual([], late_module_imports(core_guard + "import json\n"))

        self.assertEqual(
            [4],
            late_module_imports(
                core_guard + (
                    "work()\n"
                    "import json\n"
                )
            ),
        )

        sibling_guard = (
            "from .container_guard import require_container\n"
            "require_container()\n"
        )

        self.assertEqual([], late_module_imports(sibling_guard + "import json\n", package="tools"))
        self.assertEqual([3], late_module_imports(sibling_guard + "import json\n", package="other"))


class ScientificLintTests(unittest.TestCase):
    def test_provider_core_import_order(self):
        sdk_source_root = ROOT / "packages/provider-core/src"
        source_paths = source_files(root=ROOT, patterns=(f"{sdk_source_root.relative_to(ROOT)}/**/*.py",))

        self.assertTrue(source_paths, msg=f"{source_paths=} should include provider-core modules")

        findings = [
            f"{source_path.relative_to(ROOT)}:{line}: module import follows operational statements"
            for source_path in source_paths
            for line in late_module_imports(source_path.read_text(), package=".".join(source_path.parent.relative_to(sdk_source_root).parts))
        ]

        self.assertEqual([], findings, msg="\n".join(findings))

    def test_guard_exception_retains_late_module_import_rejection(self):
        # Bootstrap identity has an intentional isolated import-path setup;
        # it is qualified by the cold-bundle import tests, not this rule.
        source_paths = source_files(root=ROOT, patterns=[f"{name}/**/*.py" for name in ("host/tools", "tools", "tests", "src", "packages")])

        findings = [
            f"{source_path.relative_to(ROOT)}:{line}: module import follows operational statements"
            for source_path in sorted(source_paths)
            for line in late_module_imports(source_path.read_text(), package=".".join(source_path.parent.relative_to(ROOT).parts))
        ]

        self.assertEqual([], findings, msg="\n".join(findings))
