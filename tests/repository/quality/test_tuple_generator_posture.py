# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/test_tuple_generator_posture.py
# Original detector fixtures and repository assertions retained; source roots alone are local.
"""Require collection comprehensions to have an honest owning shape."""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path

from .sources import REPOSITORY_ROOT, python_sources

# A retained conversion must name why tuple immutability matters for that
# consumer. The tag may precede one other repository-check tag on the binding.
_ESCAPE_PREFIX = "# repo-check: allow-tuple-generator -- "


@dataclass(frozen=True, slots=True)
class _Finding:
    path: Path
    line: int
    expression: str

    def diagnostic(self) -> str:
        return (
            f"{self.path}:{self.line}: tuple() directly consumes a comprehension "
            f"{self.expression!r}; tuple may add no semantic value, generator "
            "syntax is comma-fragile, and an inline list is only a ceremonial "
            "workaround; build the collection shape its consumer actually owns, "
            f"or retain it with {_ESCAPE_PREFIX!r} and a concrete reason"
        )


def _has_documented_escape(node: ast.Call, source_lines: list[str]) -> bool:
    if node.lineno < 2:
        return False

    def documents_reason(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith(_ESCAPE_PREFIX) and bool(stripped.removeprefix(_ESCAPE_PREFIX).strip())

    immediate = source_lines[node.lineno - 2]
    if documents_reason(immediate):
        return True
    return node.lineno >= 3 and immediate.strip().startswith("# repo-check:") and documents_reason(source_lines[node.lineno - 3])


def _findings(path: Path, source: str) -> list[_Finding]:
    """Find every comprehension converted directly to a tuple."""

    tree = ast.parse(source, filename=str(path))
    source_lines = source.splitlines()
    return sorted(
        [
            _Finding(
                path=path,
                line=node.lineno,
                expression=ast.unparse(node),
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "tuple"
            and len(node.args) == 1
            and isinstance(node.args[0], (ast.GeneratorExp, ast.ListComp))
            and not node.keywords
            and not _has_documented_escape(node, source_lines)
        ],
        key=lambda finding: finding.line,
    )


def _scan(paths: list[Path]) -> list[_Finding]:
    return sorted(
        [
            finding
            for path in paths
            for finding in _findings(
                path.relative_to(REPOSITORY_ROOT),
                path.read_text(encoding="utf-8"),
            )
        ],
        key=lambda finding: (finding.path, finding.line),
    )


class TupleGeneratorPostureTests(unittest.TestCase):
    def test_detector_flags_only_direct_comprehension_conversions(self) -> None:
        source = textwrap.dedent(
            """
            direct = tuple(item.value for item in items)
            multiline = tuple(
                item.value
                for item in items
            )
            filtered = tuple(item for item in items if item.enabled)
            nested = tuple(
                item
                for group in groups
                for item in group
            )

            async def consume(items):
                return tuple(item async for item in items)

            deliberate_tuple = tuple((item.value for item in items))
            list_comprehension = tuple([item.value for item in items])
            iterable = tuple(items)
            wrapped = tuple(sorted(item.value for item in items))
            meaningful_set = set(item.value for item in items)
            meaningful_list = list(item.value for item in items)
            literal = (item.value for item in items)
            other_callable = make_tuple(item.value for item in items)
            qualified = builtins.tuple(item.value for item in items)

            # repo-check: allow-tuple-generator -- immutable protocol inventory
            allowed = tuple(item.value for item in items)

            # repo-check: allow-tuple-generator -- immutable domain inventory
            # repo-check: allow-module-constant
            ALSO_ALLOWED = tuple(item.value for item in items)
            """
        )

        findings = _findings(Path("example.py"), source)

        self.assertEqual(
            [
                (2, "tuple((item.value for item in items))"),
                (3, "tuple((item.value for item in items))"),
                (7, "tuple((item for item in items if item.enabled))"),
                (8, "tuple((item for group in groups for item in group))"),
                (15, "tuple((item async for item in items))"),
                (17, "tuple((item.value for item in items))"),
                (18, "tuple([item.value for item in items])"),
            ],
            [(finding.line, finding.expression) for finding in findings],
        )

    def test_escape_requires_a_documented_reason_near_the_binding(self) -> None:
        cases = (
            "# repo-check: allow-tuple-generator --\nvalue = tuple(x for x in xs)\n",
            "# repo-check: allow-tuple-generator\nvalue = tuple(x for x in xs)\n",
            "# repo-check: allow-tuple-generator -- immutable inventory\n\nvalue = tuple(x for x in xs)\n",
        )

        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(1, len(_findings(Path("example.py"), source)))

    def test_python_does_not_convert_comprehensions_directly_to_tuples(
        self,
    ) -> None:
        paths = python_sources(production_only=False)
        findings = _scan(paths)
        if findings:
            self.fail("tuple() calls over comprehensions:\n" + "\n".join(finding.diagnostic() for finding in findings))
