"""Keep direct call predicates after not free of redundant parentheses."""

from tools.container_guard import require_container

require_container()

import ast
import re
import unittest

from .sources import ROOT, python_sources


def redundant_negated_call_lines(*, source: str) -> list[int]:
    """Report grouping around a direct call operand, including nested conditions."""
    findings = []

    for expression in ast.walk(node=ast.parse(source=source)):
        if not isinstance(expression, ast.UnaryOp) or not isinstance(expression.op, ast.Not):
            continue

        if not isinstance(expression.operand, ast.Call):
            continue

        spelling = ast.get_source_segment(source=source, node=expression)

        if re.match(pattern=r"not\s*\(", string=spelling):
            findings.append(expression.lineno)

    return sorted(set(findings))


class NegatedCallParenthesesPostureTests(unittest.TestCase):
    def test_direct_calls_do_not_need_extra_grouping(self) -> None:
        self.assertEqual(first=[1], second=redundant_negated_call_lines(source="if not (ready()): raise ValueError"))
        self.assertEqual(first=[], second=redundant_negated_call_lines(source="if not ready(): raise ValueError"))
        self.assertEqual(first=[], second=redundant_negated_call_lines(source="if not (ready() and enabled): raise ValueError"))
        self.assertEqual(first=[1], second=redundant_negated_call_lines(source="if not (ready()) or enabled: pass"))

    def test_authored_python_omits_redundant_negated_call_parentheses(self) -> None:
        findings = [
            f"{path.relative_to(ROOT)}:{line}"
            for path in python_sources(production_only=False)
            for line in redundant_negated_call_lines(source=path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
