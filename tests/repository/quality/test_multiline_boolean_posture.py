"""Make multiline and/or expressions read as a vertical list of conditions.

Group each operand with parentheses. Direct calls, directly negated calls
and attribute reads can stand on their own. The operator ends the preceding
operand's line.
"""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from .sources import ROOT, python_sources


def multiline_boolean_findings(*, source: str) -> list[tuple[int, str]]:
    """Find multiline Boolean operands without grouping or trailing operators."""
    lines = source.encode(encoding="utf-8").split(b"\n")
    findings = []

    for expression in ast.walk(node=ast.parse(source=source)):
        if (not isinstance(expression, ast.BoolOp)) or (expression.lineno == expression.end_lineno):
            continue

        if isinstance(expression.op, ast.And):
            operator = b"and"
        else:
            operator = b"or"

        for (index, operand) in enumerate(expression.values):
            before = lines[operand.lineno - 1][:operand.col_offset].rstrip()
            after = lines[operand.end_lineno - 1][operand.end_col_offset:].lstrip()

            grouped = before.endswith(b"(") and after.startswith(b")")

            negated_call = (
                isinstance(operand, ast.UnaryOp) and
                isinstance(operand.op, ast.Not) and
                isinstance(operand.operand, ast.Call)
            )

            if not (
                (grouped) or
                (negated_call) or
                isinstance(operand, (ast.Call, ast.Attribute))
            ):
                findings.append((operand.lineno, "group each multiline Boolean operand"))
                continue

            if index < len(expression.values) - 1:
                following = after[1:] if grouped else after
                following = following.strip().split(b"#", 1)[0].strip()

                if following != operator:
                    findings.append((operand.end_lineno, "put the Boolean operator at the end of the preceding line"))

    return sorted(set(findings))


class MultilineBooleanPostureTests(unittest.TestCase):
    def test_grouped_operands_and_trailing_operators(self) -> None:
        for operator in ("and", "or"):
            with self.subTest(operator=operator):
                accepted = (
                    f"result = (\n"
                    f"    (first) {operator}\n"
                    f"    (second)\n"
                    f")\n"
                )

                leading = (
                    f"result = (\n"
                    f"    (first)\n"
                    f"    {operator} (second)\n"
                    f")\n"
                )

                ungrouped = (
                    f"result = (\n"
                    f"    first {operator}\n"
                    f"    second\n"
                    f")\n"
                )

                self.assertEqual(first=[], second=multiline_boolean_findings(source=accepted))
                self.assertTrue(expr=multiline_boolean_findings(source=leading))
                self.assertTrue(expr=multiline_boolean_findings(source=ungrouped))

        self.assertEqual(first=[], second=multiline_boolean_findings(source="result = first and second\n"))

        self.assertEqual(
            first=[],
            second=multiline_boolean_findings(
                source=(
                    "result = (\n"
                    "    ready() and\n"
                    "    (enabled)\n"
                    ")\n"
                )
            ),
        )

    def test_bare_attribute_reads_still_require_trailing_operators(self) -> None:
        self.assertEqual(
            first=[],
            second=multiline_boolean_findings(
                source=(
                    "result = (\n"
                    "    claim.active or\n"
                    "    (claim is None)\n"
                    ")\n"
                )
            ),
        )

        self.assertEqual(
            first=[(2, "put the Boolean operator at the end of the preceding line")],
            second=multiline_boolean_findings(
                source=(
                    "result = (\n"
                    "    claim.active\n"
                    "    or (claim is None)\n"
                    ")\n"
                )
            ),
        )

    def test_directly_negated_call_in_a_multiline_filter(self) -> None:
        source = (
            "unguarded = [\n"
            "    path\n"
            "    for path in paths\n"
            "    if\n"
            "    (path != excluded) and\n"
            "    not has_early_guard(\n"
            "        path.read_text(),\n"
            "        package_marker=path.name == '__init__.py',\n"
            "    )\n"
            "]\n"
        )

        self.assertEqual(first=[], second=multiline_boolean_findings(source=source))

    def test_authored_python_uses_multiline_boolean_layout(self) -> None:
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}: {reason}"
            for source_path in python_sources(production_only=False)
            for (line, reason) in multiline_boolean_findings(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
