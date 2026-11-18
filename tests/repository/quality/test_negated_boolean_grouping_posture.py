"""Make the scope of not explicit when it is combined with and or or."""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from .sources import ROOT, python_sources


def ungrouped_negated_boolean_lines(*, source: str) -> list[int]:
    """Report single-line Boolean expressions mixing ungrouped operands with not."""
    lines = source.encode(encoding="utf-8").splitlines()
    findings = []

    for expression in ast.walk(node=ast.parse(source=source)):
        if not isinstance(expression, ast.BoolOp) or expression.lineno != expression.end_lineno:
            continue

        if not any(
            (
                (isinstance(operand, ast.UnaryOp) and isinstance(operand.op, ast.Not)) or
                (isinstance(operand, ast.Compare) and any(isinstance(operator, (ast.NotIn, ast.IsNot)) for operator in operand.ops))
                for operand in expression.values
            ),
        ):
            continue

        line = lines[expression.lineno - 1]

        for operand in expression.values:
            before = line[:operand.col_offset].rstrip()
            after = line[operand.end_col_offset:].lstrip()

            if not (before.endswith(b"(") and after.startswith(b")")):
                findings.append(expression.lineno)

                break

    return sorted(set(findings))


class NegatedBooleanGroupingPostureTests(unittest.TestCase):
    def test_negated_operands_need_explicit_grouping(self) -> None:
        self.assertEqual(first=[1], second=ungrouped_negated_boolean_lines(source='if not name or "x" in name: pass'))
        self.assertEqual(first=[], second=ungrouped_negated_boolean_lines(source='if (not name) or ("x" in name): pass'))
        self.assertEqual(first=[1], second=ungrouped_negated_boolean_lines(source='if not (name and "x" not in name): pass'))
        self.assertEqual(first=[], second=ungrouped_negated_boolean_lines(source='if not ((name) and ("x" not in name)): pass'))
        self.assertEqual(first=[], second=ungrouped_negated_boolean_lines(source="if ready or waiting: pass"))

    def test_authored_python_groups_negated_boolean_operands(self) -> None:
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}"
            for source_path in python_sources(production_only=False)
            for line in ungrouped_negated_boolean_lines(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
