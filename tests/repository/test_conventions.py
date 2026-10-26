"""Check repository conventions against authored sources and maintained targets.

NMRPeak supplies the hierarchical test aggregate; Magnet supplies recursive
production-source inventories with narrow, documented ownership exceptions:
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/Makefile#L87-L102
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/tests/repository/test_provider_event_logging_posture.py#L34-L94
This suite inspects source without importing scientific dependencies. Its own
execution remains inside the maintained container, just like the unit suite.
"""

from tools.container_guard import require_container

require_container()

import ast
import re
import tempfile
import unittest
from pathlib import Path

from tests.repository.quality.sources import ROOT


# silent-defaults: allow-contract -- Ordinary modules require the guard; the explicit package-marker option permits only empty or docstring-only markers; proof=tests/repository/test_conventions.py::ConventionDetectionTests.test_only_empty_or_documentation_package_markers_are_exempt
def has_early_guard(source, *, package_marker=False, guard_module="tools.container_guard"):
    """Recognize the canonical guard before imports or executable statements.

    Empty/docstring-only package markers are a local exception: importing them
    cannot start work. An initializer with imports or other statements must guard.
    """
    statements = ast.parse(source).body
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    if package_marker and not statements:
        return True
    while (
        statements
        and isinstance(statements[0], ast.ImportFrom)
        and statements[0].level == 0
        and statements[0].module == "__future__"
    ):
        statements = statements[1:]
    if len(statements) < 2:
        return False
    imported, called = statements[:2]
    return (
        isinstance(imported, ast.ImportFrom)
        and imported.level == 0
        and imported.module == guard_module
        and len(imported.names) == 1
        and imported.names[0].name == "require_container"
        and imported.names[0].asname is None
        and isinstance(called, ast.Expr)
        and isinstance(called.value, ast.Call)
        and isinstance(called.value.func, ast.Name)
        and called.value.func.id == "require_container"
        and not called.value.args
        and not called.value.keywords
    )


def unguarded_sources(root):
    """Include new production files automatically, without scanning upstream or fixtures."""
    sources = {path for name in ("tools", "tests/repository", "src") for path in (root / name).rglob("*.py")}
    # The shared guard must be importable before its caller checks the marker.
    sources.discard(root / "tools" / "container_guard.py")
    failures = [
        str(path.relative_to(root))
        for path in sorted(sources)
        if not has_early_guard(path.read_text(), package_marker=path.name == "__init__.py")
    ]
    core_root = root / "packages/provider-core/src/provider_core"
    failures.extend(
        str(path.relative_to(root))
        for path in sorted(core_root.rglob("*.py"))
        if path != core_root / "container_guard.py"
        and not has_early_guard(
            path.read_text(), package_marker=path.name == "__init__.py", guard_module="provider_core.container_guard"
        )
    )
    return failures


def make_targets(source):
    """Read literal root-Makefile targets; recipes and variable assignments are excluded."""
    targets = {}
    for line in source.splitlines():
        match = re.match(r"^([A-Za-z0-9_./% -]+):(?!=)(.*)$", line)
        if match and not match[1].startswith("."):
            for name in match[1].split():
                targets[name] = match[2].split("#", 1)[0].split()
    return targets


class RepositoryConventionTests(unittest.TestCase):
    def test_authored_production_sources_require_the_shared_guard(self):
        self.assertTrue(list((ROOT / "tools").rglob("*.py")), msg="production source inventory is empty")
        self.assertEqual([], unguarded_sources(ROOT))

    def test_public_make_targets_are_hierarchical_and_test_aggregates_both_suites(self):
        targets = make_targets((ROOT / "Makefile").read_text())
        self.assertTrue(targets, msg="Makefile target inventory is empty")
        for name in targets:
            if name not in ("tests", "help"):
                self.assertRegex(name, r"^[a-z][a-z0-9_-]*(/[a-z][a-z0-9_-]*)+$")
        self.assertTrue({"tests/repository"} <= targets.keys())
        self.assertTrue({"tests/repository"} <= set(targets["tests"]))


class ConventionDetectionTests(unittest.TestCase):
    GUARD = "from tools.container_guard import require_container\nrequire_container()\n"

    def test_installed_core_requires_its_own_portable_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "packages/provider-core/src/provider_core/worker.py"
            worker.parent.mkdir(parents=True)
            worker.write_text(self.GUARD + "import json\n")
            self.assertEqual([str(worker.relative_to(root))], unguarded_sources(root))
            worker.write_text("from provider_core.container_guard import require_container\nrequire_container()\nimport json\n")
            self.assertEqual([], unguarded_sources(root))

    def test_missing_late_conditional_and_rebound_guards_fail(self):
        for source in (
            "import pickle\n",
            "import pickle\n" + self.GUARD,
            "from .__future__ import annotations\n" + self.GUARD,
            "if True:\n    from tools.container_guard import require_container\n    require_container()\n",
            "from tools.container_guard import require_container\nrequire_container = lambda: None\nrequire_container()\n",
        ):
            with self.subTest(source=source):
                self.assertFalse(has_early_guard(source))

    def test_canonical_guard_allows_module_documentation_and_future_import(self):
        source = '"""Worker responsibility."""\nfrom __future__ import annotations\n' + self.GUARD + "import pickle\n"
        self.assertTrue(has_early_guard(source))

    def test_only_empty_or_documentation_package_markers_are_exempt(self):
        for source in ("", '"""Package marker."""\n'):
            self.assertTrue(has_early_guard(source, package_marker=True))
            self.assertFalse(has_early_guard(source))
        self.assertFalse(has_early_guard("import pickle\n", package_marker=True))
        self.assertTrue(has_early_guard(self.GUARD + "import pickle\n", package_marker=True))

    def test_new_nested_workers_and_future_package_enter_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "tools/container_guard.py",
                "tools/new/worker.py",
                "src/model.py",
                "src/input_geometry.py",
                "tests/repository/nested/test_new.py",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("import pickle\n")
            self.assertEqual(
                [
                    "src/input_geometry.py",
                    "src/model.py",
                    "tests/repository/nested/test_new.py",
                    "tools/new/worker.py",
                ],
                unguarded_sources(root),
            )
            (root / "tools/new/worker.py").write_text(self.GUARD)
            self.assertEqual(
                ["src/input_geometry.py", "src/model.py", "tests/repository/nested/test_new.py"],
                unguarded_sources(root),
            )

    def test_target_parser_ignores_recipes_and_assignments(self):
        targets = make_targets(
            ".PHONY: test test/unit\nROOT := /workspace\ntest: test/unit test/repository\n\techo unexpected: text\ntest/unit test/repository:\n"
        )
        self.assertEqual({"test": ["test/unit", "test/repository"], "test/unit": [], "test/repository": []}, targets)


if __name__ == "__main__":
    unittest.main()
