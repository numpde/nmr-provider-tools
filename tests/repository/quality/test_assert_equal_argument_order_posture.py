# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/test_assert_equal_argument_order_posture.py
# Original detector fixtures and repository assertions retained; source roots alone are local.
"""Flag ambiguous argument shapes in unittest assertion calls."""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import inspect
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path

from .sources import REPOSITORY_ROOT


@dataclass(frozen=True, slots=True)
class _Finding:
    path: Path
    line: int

    def diagnostic(self) -> str:
        return (
            f"{self.path}:{self.line}: assertEqual() has an observation-shaped "
            "first argument and a literal expectation second; consider "
            "assertEqual(expected, observed)"
        )


@dataclass(frozen=True, slots=True)
class _PositionalMessageFinding:
    path: Path
    line: int
    method: str

    def diagnostic(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.method}() passes its diagnostic "
            "message positionally; use msg= so the argument's role is explicit"
        )


def _is_literal(expression: ast.expr) -> bool:
    if isinstance(expression, ast.Constant):
        return True
    if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal(element) for element in expression.elts)
    if isinstance(expression, ast.Dict):
        return all(
            key is not None and _is_literal(key) and _is_literal(value)
            for key, value in zip(expression.keys, expression.values, strict=True)
        )
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, (ast.Invert, ast.UAdd, ast.USub)):
        return _is_literal(expression.operand)
    return False


def _is_assert_equal(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and call.func.attr == "assertEqual"
        and len(call.args) >= 2
    )


def _findings(path: Path, source: str) -> list[_Finding]:
    tree = ast.parse(source, filename=str(path))
    return sorted(
        [
            _Finding(path=path, line=call.lineno)
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and _is_assert_equal(call)
            and isinstance(call.args[0], (ast.Attribute, ast.Call, ast.Subscript))
            and _is_literal(call.args[1])
        ],
        key=lambda finding: finding.line,
    )


def _message_positions() -> dict[str, int]:
    positions: dict[str, int] = {}
    for method_name in dir(unittest.TestCase):
        if not method_name.startswith("assert"):
            continue
        method = getattr(unittest.TestCase, method_name)
        if not callable(method):
            continue
        positional_parameters = [
            parameter
            for parameter in inspect.signature(method).parameters.values()
            if parameter.name != "self"
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        for position, parameter in enumerate(positional_parameters):
            if parameter.name == "msg":
                positions[method_name] = position
                break
    return positions


_message_position_by_method = _message_positions()


def _positional_message_findings(
    path: Path,
    source: str,
) -> list[_PositionalMessageFinding]:
    tree = ast.parse(source, filename=str(path))
    findings: list[_PositionalMessageFinding] = []
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
        ):
            continue
        message_position = _message_position_by_method.get(call.func.attr)
        if message_position is not None and len(call.args) > message_position:
            findings.append(
                _PositionalMessageFinding(
                    path=path,
                    line=call.lineno,
                    method=call.func.attr,
                )
            )
    return sorted(findings, key=lambda finding: finding.line)


def _test_paths() -> list[Path]:
    roots = [REPOSITORY_ROOT / "repository_checks", REPOSITORY_ROOT / "tests"]
    return sorted(path for root in roots for path in root.rglob("*.py"))


class AssertEqualArgumentOrderPostureTests(unittest.TestCase):
    def test_detector_flags_only_visible_expected_second_arguments(self) -> None:
        source = textwrap.dedent(
            """
            self.assertEqual(load_items(), [])
            self.assertEqual(response["status"], 200)
            self.assertEqual(result.status, {"ready": [1, -2]})

            self.assertEqual([], load_items())
            self.assertEqual("ready", result)
            self.assertEqual(result, "ready")
            self.assertEqual(actual, EXPECTED_RESULT)
            self.assertEqual(expected_value, value)
            self.assertEqual(left, right)
            self.assertEqual(load_items(), [expected_item])
            self.assertEqual(load_items(), make_expected())
            other.assertEqual(load_items(), [])
            self.assertNotEqual(load_items(), [])
            """
        )

        self.assertEqual(
            [2, 3, 4],
            [finding.line for finding in _findings(Path("example.py"), source)],
        )

    def test_message_detector_uses_unittest_signatures(self) -> None:
        source = textwrap.dedent(
            """
            self.assertEqual(expected, observed, "context")
            self.assertTrue(condition, "context")
            self.assertAlmostEqual(expected, observed, 3, "context")

            self.assertEqual(expected, observed, msg="context")
            self.assertTrue(condition, msg="context")
            self.assertAlmostEqual(expected, observed, 3)
            self.assertAlmostEqual(
                expected,
                observed,
                places=3,
                msg="context",
            )
            self.assertRaises(ValueError, operation, argument)
            other.assertEqual(expected, observed, "context")
            """
        )

        self.assertEqual(
            [(2, "assertEqual"), (3, "assertTrue"), (4, "assertAlmostEqual")],
            [(finding.line, finding.method) for finding in _positional_message_findings(Path("example.py"), source)],
        )

    def test_repository_assert_equal_calls_put_visible_expectations_first(
        self,
    ) -> None:
        findings = sorted(
            [
                finding
                for path in _test_paths()
                for finding in _findings(
                    path.relative_to(REPOSITORY_ROOT),
                    path.read_text(encoding="utf-8"),
                )
            ],
            key=lambda finding: (finding.path, finding.line),
        )
        if findings:
            self.fail(
                "assertEqual() calls with visibly reversed arguments:\n" + "\n".join(finding.diagnostic() for finding in findings)
            )

    def test_repository_assertion_messages_are_keyword_arguments(self) -> None:
        findings = sorted(
            [
                finding
                for path in _test_paths()
                for finding in _positional_message_findings(
                    path.relative_to(REPOSITORY_ROOT),
                    path.read_text(encoding="utf-8"),
                )
            ],
            key=lambda finding: (finding.path, finding.line, finding.method),
        )
        if findings:
            self.fail(
                "unittest assertion calls with positional diagnostic messages:\n"
                + "\n".join(finding.diagnostic() for finding in findings)
            )
