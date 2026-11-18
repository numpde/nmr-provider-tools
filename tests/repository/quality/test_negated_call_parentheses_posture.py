"""Keep direct call predicates after not free of redundant parentheses."""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from .sources import ROOT, python_sources


def redundant_negated_call_lines(*, source: str) -> list[int]:
    """Report grouping around a direct call operand, including nested conditions."""
    findings = []

    for expression in ast.walk(node=ast.parse(source=source)):
        if (not isinstance(expression, ast.UnaryOp)) or (not isinstance(expression.op, ast.Not)):
            continue

        if not isinstance(expression.operand, ast.Call):
            continue

        # Outer grouping extends the negation past the end of the call
        if (expression.end_lineno, expression.end_col_offset) != (
            expression.operand.end_lineno,
            expression.operand.end_col_offset,
        ):
            findings.append(expression.lineno)

    return sorted(set(findings))


class NegatedCallParenthesesPostureTests(unittest.TestCase):
    def test_direct_calls_do_not_need_extra_grouping(self) -> None:
        self.assertEqual(first=[1], second=redundant_negated_call_lines(source="if not (ready()): raise ValueError"))
        self.assertEqual(first=[], second=redundant_negated_call_lines(source="if not ready(): raise ValueError"))
        self.assertEqual(first=[], second=redundant_negated_call_lines(source="if not (ready() and enabled): raise ValueError"))
        self.assertEqual(first=[1], second=redundant_negated_call_lines(source="if not (ready()) or enabled: pass"))
        self.assertEqual(first=[], second=redundant_negated_call_lines(source="if not (root / name).exists(): pass"))
        self.assertEqual(first=[1], second=redundant_negated_call_lines(source="if not ((root / name).exists()): pass"))

    def test_authored_python_omits_redundant_negated_call_parentheses(self) -> None:
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}"
            for source_path in python_sources(production_only=False)
            for line in redundant_negated_call_lines(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
