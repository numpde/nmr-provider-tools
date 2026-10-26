# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/test_repository_root_posture.py
# Original detector fixtures and repository assertions retained; source roots alone are local.
"""Reject repository-root inference from incidental Python source layout."""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path

from .sources import REPOSITORY_ROOT, python_sources

_CHECK_PATH = Path(__file__).resolve()


@dataclass(frozen=True, slots=True)
class _Finding:
    path: Path
    line: int
    target: str
    expression: str

    def diagnostic(self) -> str:
        return (
            f"{self.path}:{self.line}: repository root {self.target!r} is "
            f"inferred from source layout by {self.expression!r}; use Git "
            "discovery or one pinned repository file"
        )


def _assigned_names(statement: ast.stmt) -> list[str]:
    if isinstance(statement, ast.Assign):
        return [target.id for target in statement.targets if isinstance(target, ast.Name)]
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return (statement.target.id,)
    return []


def _assigned_value(statement: ast.stmt) -> ast.expr | None:
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        return statement.value
    return None


def _repository_root_name(name: str) -> str | None:
    normalized = name.lstrip("_").casefold()
    if normalized in {
        "repo_root",
        "repository_root",
        "project_root",
    }:
        return normalized
    if normalized == "root":
        return normalized
    return None


def _source_location_depth(
    expression: ast.expr,
    source_location_depths: dict[str, int],
) -> int | None:
    origin_depths: list[int] = []
    for node in ast.walk(expression):
        if isinstance(node, ast.Name) and node.id == "__file__":
            origin_depths.append(0)
        elif isinstance(node, ast.Name) and node.id in source_location_depths:
            origin_depths.append(source_location_depths[node.id])
        if isinstance(node, ast.Attribute) and node.attr == "__file__":
            origin_depths.append(0)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "inspect"
            and node.func.attr == "getfile"
        ):
            origin_depths.append(0)
    if not origin_depths:
        return None
    return max(origin_depths) + _upward_steps(expression)


def _upward_steps(expression: ast.expr) -> int:
    steps = 0
    for node in ast.walk(expression):
        if isinstance(node, ast.Subscript) and (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents"
            and isinstance(node.slice, ast.Constant)
            and type(node.slice.value) is int
            and node.slice.value >= 0
        ):
            steps = max(steps, node.slice.value + 1)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            consecutive_steps = 1
            parent = node.value
            while isinstance(parent, ast.Attribute) and parent.attr == "parent":
                consecutive_steps += 1
                parent = parent.value
            steps = max(steps, consecutive_steps)
    return steps


def _root_target_uses_source_layout(name: str, depth: int) -> bool:
    normalized = _repository_root_name(name)
    if normalized is None:
        return False
    if normalized == "root":
        return depth >= 2
    return depth >= 1


def _findings_in_body(
    *,
    path: Path,
    statements: list[ast.stmt],
    inherited_source_location_depths: dict[str, int] | None = None,
) -> tuple[_Finding, ...]:
    findings: list[_Finding] = []
    # silent-defaults: allow-contract -- A top-level body starts with no source-location bindings; nested bodies copy the inherited scope before updating their own map; proof=tests/repository/quality/test_repository_root_posture.py::RepositoryRootPostureTests.test_detector_flags_source_layout_repository_root_inference
    source_location_depths = dict(inherited_source_location_depths or {})
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            findings.extend(
                _findings_in_body(
                    path=path,
                    statements=statement.body,
                    inherited_source_location_depths=source_location_depths,
                )
            )
        names = _assigned_names(statement)
        value = _assigned_value(statement)
        if value is None or not names:
            continue
        source_location_depth = _source_location_depth(
            value,
            source_location_depths,
        )
        if source_location_depth is not None:
            findings.extend(
                _Finding(
                    path=path,
                    line=statement.lineno,
                    target=name,
                    expression=ast.unparse(value),
                )
                for name in names
                if _root_target_uses_source_layout(name, source_location_depth)
            )
        for name in names:
            if source_location_depth is not None:
                source_location_depths[name] = source_location_depth
            else:
                # silent-defaults: allow-contract -- Non-source rebinding removes that name's earlier source-layout provenance; an untracked name needs no removal; proof=tests/repository/quality/test_repository_root_posture.py::RepositoryRootPostureTests.test_detector_ignores_non_repository_and_non_source_roots
                source_location_depths.pop(name, None)
    return tuple(findings)


def _findings(path: Path, source: str) -> tuple[_Finding, ...]:
    tree = ast.parse(source, filename=str(path))
    return _findings_in_body(path=path, statements=tree.body)


def _scan(paths: tuple[Path, ...]) -> tuple[_Finding, ...]:
    return tuple(
        sorted(
            (
                finding
                for path in paths
                for finding in _findings(
                    path.relative_to(REPOSITORY_ROOT),
                    path.read_text(encoding="utf-8"),
                )
            ),
            key=lambda finding: (finding.path, finding.line),
        )
    )


class RepositoryRootPostureTests(unittest.TestCase):
    def test_detector_flags_source_layout_repository_root_inference(self) -> None:
        source = textwrap.dedent(
            """
            ROOT = Path(__file__).resolve().parents[1]

            TEST_ROOT = Path(__file__).resolve().parents[1]
            REPO_ROOT = TEST_ROOT.parent

            def inspect_package(package):
                package_root = Path(inspect.getfile(package)).parent
                repo_root = package_root.parents[1]
                return repo_root

            HERE = Path(__file__).resolve()

            def nested_repository_root():
                repository_root = HERE.parents[1]
                return repository_root
            """
        )

        findings = _findings(Path("example.py"), source)

        self.assertEqual(
            [
                ("ROOT", 2),
                ("REPO_ROOT", 5),
                ("repo_root", 9),
                ("repository_root", 15),
            ],
            [(finding.target, finding.line) for finding in findings],
        )

    def test_detector_ignores_non_repository_and_non_source_roots(self) -> None:
        source = textwrap.dedent(
            """
            PACKAGE_ROOT = Path(__file__).resolve().parent
            TEST_ROOT = Path(__file__).resolve().parents[1]
            ROOT = Path(__file__).resolve().parent
            fixture_root = some_path.parents[1]
            ROOT = pinned_repository_file.parent
            REPO_ROOT = discover_with_git()
            PROJECT_ROOT = Path.cwd().parent
            HERE = Path(__file__).resolve().parents[1]
            HERE = pinned_repository_file
            REPO_ROOT = HERE.parent
            """
        )

        self.assertEqual((), _findings(Path("example.py"), source))

    def test_check_uses_no_source_layout_repository_root_inference(self) -> None:
        self.assertEqual((), _scan((_CHECK_PATH,)))

    def test_python_does_not_infer_repository_root_from_source_layout(self) -> None:
        paths = tuple(python_sources(production_only=False))
        findings = _scan(paths)
        if findings:
            self.fail("repository roots inferred from source layout:\n" + "\n".join(finding.diagnostic() for finding in findings))
