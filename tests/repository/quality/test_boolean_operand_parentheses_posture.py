"""Group operator expressions used by and/or, including single-line conditions.

Comparisons and other compound operands need their own parentheses. Atoms,
calls and directly negated calls stay compact. Multiline layout has additional
rules in test_multiline_boolean_posture.py.
"""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from .sources import ROOT, python_sources
from .expression_grouping import needs_operator_grouping, ungrouped_expression_findings


def _boolean_operand_needs_grouping(operand: ast.expr) -> bool:
    """Keep the established exception for directly negated call predicates."""

    if (
        isinstance(operand, ast.UnaryOp) and
        isinstance(operand.op, ast.Not) and
        isinstance(operand.operand, ast.Call)
    ):
        return False

    return needs_operator_grouping(operand)


def boolean_operand_findings(*, source: str) -> list[tuple[int, int, str]]:
    """Locate complete Boolean operands whose operator scope is ungrouped."""

    expressions = (
        operand
        for expression in ast.walk(node=ast.parse(source=source))
        if isinstance(expression, ast.BoolOp)
        for operand in expression.values
        if _boolean_operand_needs_grouping(operand)
    )

    return ungrouped_expression_findings(source=source, expressions=expressions)


class BooleanOperandParenthesesPostureTests(unittest.TestCase):
    def test_comparisons_need_grouping_for_both_boolean_operators(self):
        for operator in ("and", "or"):
            with self.subTest(operator=operator):
                self.assertEqual(
                    ["first == second", "third != fourth"],
                    [operand for (_, _, operand) in boolean_operand_findings(source=f"first == second {operator} third != fourth\n")],
                )

                self.assertEqual([], boolean_operand_findings(source=f"(first == second) {operator} (third != fourth)\n"))

    def test_reported_fixture_and_route_conditions_are_flagged(self):
        cases = (
            (
                'if target.name == ("status." + phase) or (target.name == "status" and phase == "status"): pass\n',
                'if (target.name == ("status." + phase)) or ((target.name == "status") and (phase == "status")): pass\n',
                3,
            ),
            (
                'if len(parts) >= 10 and parts[0] == ("0" * 32) and parts[1] == "00" and parts[-1] != "lo": pass\n',
                'if (len(parts) >= 10) and (parts[0] == ("0" * 32)) and (parts[1] == "00") and (parts[-1] != "lo"): pass\n',
                4,
            ),
        )

        for (ungrouped, grouped, expected_count) in cases:
            with self.subTest(source=ungrouped):
                self.assertEqual(expected_count, len(boolean_operand_findings(source=ungrouped)))
                self.assertEqual([], boolean_operand_findings(source=grouped))

    def test_negation_arithmetic_and_nested_boolean_scope_need_grouping(self):
        cases = (
            ("not name or waiting", ["not name"]),
            ("not owner.active and waiting", ["not owner.active"]),
            ("first + second and ready", ["first + second"]),
            ("~mask or ready", ["~mask"]),
            ("first and second or ready", ["first and second"]),
            ('not (name and "x" not in name)', ['"x" not in name']),
        )

        for (source, expected_operands) in cases:
            with self.subTest(source=source):
                self.assertEqual(expected_operands, [operand for (_, _, operand) in boolean_operand_findings(source=source)])

        self.assertEqual([], boolean_operand_findings(source='not ((name) and ("x" not in name))'))
        self.assertEqual([], boolean_operand_findings(source='(not name) or ("x" in name)'))

    def test_atoms_and_direct_call_predicates_stay_compact(self):
        source = (
            "first and second\n"
            "owner.ready or entries[index]\n"
            "ready() and not blocked()\n"
            "not inspect(obj=source) or owner.active\n"
            "-1 and +2\n"
            "-1j or ready\n"
            "(first and second) or third\n"
            "(first if choose else second) and ready\n"
            "(captured := produce()) or ready\n"
        )

        self.assertEqual([], boolean_operand_findings(source=source))

    def test_surrounding_statement_call_and_child_groups_are_insufficient(self):
        for source in (
            "if (first == second and ready): pass\n",
            "consume(first == second and ready)\n",
            "(first) == (second) and ready\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(1, len(boolean_operand_findings(source=source)))

    def test_multiline_comments_unicode_and_fstring_conditions(self):
        source = (
            "résultat = (\n"
            "    (préfixe in noms) and  # ) is only comment text\n"
            "    not bloqué()\n"
            ")\n"
            'message = f"{préfixe in noms and prêt}"\n'
            'message = f"{(préfixe in noms) and prêt}"\n'
        )

        self.assertEqual(["préfixe in noms"], [operand for (_, _, operand) in boolean_operand_findings(source=source)])

    def test_authored_boolean_operands_group_operator_expressions(self):
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}:{column}: parenthesize Boolean operand {operand!r}"
            for source_path in python_sources(production_only=False)
            for (line, column, operand) in boolean_operand_findings(source=source_path.read_text(encoding="utf-8"))
        ]

        if findings:
            self.fail(f"{len(findings)} unparenthesized Boolean operands:\n" + "\n".join(findings))
