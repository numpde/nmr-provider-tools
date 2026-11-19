"""Group operator expressions produced by comprehensions.

Check generator, list and set elements, and both dictionary keys and values.
The comprehension's delimiters do not group its result expression. Atoms,
calls, signed numeric literals and directly negated call predicates stay compact.
"""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from .sources import ROOT, python_sources
from .expression_grouping import needs_operator_grouping, ungrouped_expression_findings


def comprehension_result_findings(*, source: str) -> list[tuple[int, int, str]]:
    """Locate ungrouped operators in produced expressions, excluding filters."""

    expressions = []

    for comprehension in ast.walk(node=ast.parse(source=source)):
        if isinstance(comprehension, ast.DictComp):
            results = (comprehension.key, comprehension.value)
        elif isinstance(comprehension, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            results = (comprehension.elt,)
        else:
            continue

        for result in results:
            if (
                isinstance(result, ast.UnaryOp) and
                isinstance(result.op, ast.Not) and
                isinstance(result.operand, ast.Call)
            ):
                continue

            if needs_operator_grouping(result):
                expressions.append(result)

    return ungrouped_expression_findings(source=source, expressions=expressions)


class ComprehensionResultParenthesesPostureTests(unittest.TestCase):
    def test_reported_surrogate_range_needs_its_own_group(self):
        source = 'if ("\\0" in value) or any(0xD800 <= ord(character) <= 0xDFFF for character in value): pass\n'

        self.assertEqual(
            ["0xD800 <= ord(character) <= 0xDFFF"],
            [expression for (_, _, expression) in comprehension_result_findings(source=source)],
        )
        self.assertEqual(
            [],
            comprehension_result_findings(source='any((0xD800 <= ord(character) <= 0xDFFF) for character in value)\n'),
        )

    def test_all_comprehension_kinds_check_their_results(self):
        for source in (
            "(left + right for left in entries)",
            "[left + right for left in entries]",
            "{left + right for left in entries}",
            "{left + right: item for left in entries}",
            "{item: left + right for left in entries}",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    ["left + right"],
                    [expression for (_, _, expression) in comprehension_result_findings(source=source)],
                )

    def test_operator_kinds_and_partial_grouping(self):
        for expression in ("(left) + (right)", "left and right", "not ready", "~mask", "-limit", "left if ready else right"):
            with self.subTest(expression=expression):
                self.assertEqual(1, len(comprehension_result_findings(source=f"({expression} for item in entries)")))
                self.assertEqual([], comprehension_result_findings(source=f"(({expression}) for item in entries)"))

    def test_atoms_calls_signed_literals_and_filters_stay_compact(self):
        for expression in ("item", "item.name", "item[0]", "read(item)", "not valid(item)", "-1", "+2", "-1j"):
            with self.subTest(expression=expression):
                self.assertEqual(
                    [],
                    comprehension_result_findings(source=f"[{expression} for item in entries if item != excluded]"),
                )

        self.assertEqual([], comprehension_result_findings(source="{key: (item + offset) for key, item in entries}"))
        self.assertEqual([], comprehension_result_findings(source="[(captured := read(item)) for item in entries]"))

    def test_nested_comprehensions_are_checked_independently(self):
        self.assertEqual(
            ["item + offset"],
            [expression for (_, _, expression) in comprehension_result_findings(source="[[item + offset for item in row] for row in rows]")],
        )

    def test_multiline_comments_unicode_and_fstrings(self):
        source = (
            "résultat = [\n"
            "    (préfixe +  # A comment is not a closing delimiter: )\n"
            "     nom)\n"
            "    for nom in noms\n"
            "]\n"
            "autre = [préfixe + nom for nom in noms]\n"
            'message = f"{any(préfixe == nom for nom in noms)}"\n'
            'message = f"{any((préfixe == nom) for nom in noms)}"\n'
        )

        self.assertEqual(
            ["préfixe + nom", "préfixe == nom"],
            [expression for (_, _, expression) in comprehension_result_findings(source=source)],
        )

    def test_authored_comprehension_results_group_operator_expressions(self):
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}:{column}: parenthesize comprehension result {expression!r}"
            for source_path in python_sources(production_only=False)
            for (line, column, expression) in comprehension_result_findings(source=source_path.read_text(encoding="utf-8"))
        ]

        if findings:
            self.fail(f"{len(findings)} unparenthesized comprehension results:\n" + "\n".join(findings))
