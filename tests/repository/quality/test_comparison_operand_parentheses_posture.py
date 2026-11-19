"""Parenthesize operator expressions used as comparison operands.

Apply to both sides of every comparison, including intermediate operands in
comparison chains. Binary, unary, Boolean, conditional, assignment and nested
comparison expressions need their own grouping. Names, attributes,
subscriptions, calls and signed numeric literals stay compact.
Grouping the comparison itself does not group its operands.
"""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from .sources import ROOT, python_sources
from .expression_grouping import needs_operator_grouping, ungrouped_expression_findings


def comparison_operand_findings(*, source: str) -> list[tuple[int, int, str]]:
    """Locate ungrouped operands using matched delimiters and AST source spans."""

    expressions = (
        operand
        for comparison in ast.walk(node=ast.parse(source=source))
        if isinstance(comparison, ast.Compare)
        for operand in (comparison.left, *comparison.comparators)
        if needs_operator_grouping(operand)
    )

    return ungrouped_expression_findings(source=source, expressions=expressions)


class ComparisonOperandParenthesesPostureTests(unittest.TestCase):
    def test_every_comparison_operator_checks_both_sides(self):
        for operator in ("==", "!=", "<", "<=", ">", ">=", "is", "is not", "in", "not in"):
            with self.subTest(operator=operator):
                source = f"left + offset {operator} required | optional\n"

                self.assertEqual(
                    ["left + offset", "required | optional"],
                    [operand for (_, _, operand) in comparison_operand_findings(source=source)],
                )
                self.assertEqual([], comparison_operand_findings(source=f"(left + offset) {operator} (required | optional)\n"))

    def test_arithmetic_bitwise_and_unary_expressions_need_grouping(self):
        operands = (
            '"provider_core/" + leaf',
            "left - right",
            "left * right",
            "left @ right",
            "left / right",
            "left // right",
            "left % right",
            "left ** right",
            "left << right",
            "left >> right",
            "left & right",
            "left ^ right",
            "left | right",
            "-limit",
            "+limit",
            "~mask",
            "--1",
            "+True",
        )

        for operand in operands:
            with self.subTest(operand=operand):
                findings = comparison_operand_findings(source=f"{operand} in members\n")

                self.assertEqual([operand], [expression for (_, _, expression) in findings])
                self.assertEqual([], comparison_operand_findings(source=f"({operand}) in members\n"))

    def test_chains_check_intermediate_operands_and_nested_comparisons(self):
        source = "lower + margin < measured * scale < upper - margin\n"

        self.assertEqual(
            ["lower + margin", "measured * scale", "upper - margin"],
            [operand for (_, _, operand) in comparison_operand_findings(source=source)],
        )
        self.assertEqual([], comparison_operand_findings(source="(lower + margin) < (measured * scale) < (upper - margin)\n"))
        self.assertEqual(
            ["left + right"],
            [operand for (_, _, operand) in comparison_operand_findings(source="((left + right < limit) + offset) == expected\n")],
        )

    def test_comparison_call_and_partial_grouping_do_not_group_the_operand(self):
        sources = (
            "if (left + right in members): pass\n",
            "consume(left + right in members)\n",
            "consume(expected == left + right)\n",
            "((left) + (right) in members)\n",
            "any(\"provider_core/\" + leaf in member_names for leaf in helper_files)\n",
        )

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(1, len(comparison_operand_findings(source=source)))

    def test_atoms_calls_and_signed_literals_need_no_extra_grouping(self):
        source = (
            "name in names\n"
            "owner.name == expected\n"
            "entries[index] != expected\n"
            "len(entries) >= 1\n"
            "(root / name).exists() == expected\n"
            "(first + second).real == expected\n"
            "values[first + second] == expected\n"
            "-1 <= offset < +2\n"
            "measurement > -1.5\n"
            "phase == -1j\n"
            "expected in [left + right]\n"
            "expected == {left | right}\n"
        )

        self.assertEqual([], comparison_operand_findings(source=source))

    def test_lower_precedence_operands_keep_their_required_parentheses(self):
        source = (
            "(ready and enabled) == expected\n"
            "expected == (ready or enabled)\n"
            "(primary if ready else secondary) in options\n"
            "(captured := produce()) is expected\n"
            "(left < right) == expected\n"
            "expected == (not ready)\n"
        )

        self.assertEqual([], comparison_operand_findings(source=source))

    def test_multiline_comparison_grouping_does_not_group_its_operand(self):
        source = (
            "if (\n"
            "    first +\n"
            "    second in members\n"
            "):\n"
            "    pass\n"
        )

        self.assertEqual(
            [
                (
                    2,
                    5,
                    "first +\n"
                    "    second",
                ),
            ],
            comparison_operand_findings(source=source),
        )

    def test_comments_multiline_operands_unicode_and_fstrings(self):
        source = (
            "résultat = (\n"
            "    (\n"
            "        préfixe +  # A comment cannot close this group: )\n"
            "        nom\n"
            "    ) in membres\n"
            ")\n"
            "autre = préfixe + nom in membres\n"
            'message = f"{(préfixe + nom) in membres}"\n'
            'message = f"{préfixe + nom in membres}"\n'
            'literal = "left + right in members"\n'
        )

        self.assertEqual(
            [(7, 9, "préfixe + nom"), (9, 14, "préfixe + nom")],
            comparison_operand_findings(source=source),
        )

    def test_authored_comparison_operands_group_operator_expressions(self):
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}:{column}: parenthesize comparison operand {operand!r}"
            for source_path in python_sources(production_only=False)
            for (line, column, operand) in comparison_operand_findings(source=source_path.read_text(encoding="utf-8"))
        ]

        if findings:
            self.fail(f"{len(findings)} unparenthesized comparison operands:\n" + "\n".join(findings))
