"""Keep a single-line exception argument on the same line as its raise.

Only a sole argument already written on one physical line is covered. Calls
with multiple arguments, multiline expressions, comments or explicit chaining
retain their existing layout.
"""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from .sources import ROOT, python_sources


def unnecessary_raise_breaks(*, source: str) -> list[int]:
    """Find multiline raises whose sole argument can retain its exact spelling."""
    findings = []
    lines = source.splitlines()

    for node in ast.walk(node=ast.parse(source=source)):
        if not isinstance(node, ast.Raise) or node.cause is not None or not isinstance(node.exc, ast.Call):
            continue

        call = node.exc

        if len(call.args) != 1 or call.keywords or call.lineno == call.end_lineno:
            continue

        argument = call.args[0]

        if argument.lineno != argument.end_lineno or call.func.lineno != call.func.end_lineno:
            continue

        # Comments and blank lines may explain the expression or affect its layout.
        if call.end_lineno != call.lineno + 2 or any("#" in line for line in lines[call.lineno - 1:call.end_lineno]):
            continue

        findings.append(node.lineno)

    return sorted(findings)


class SingleArgumentRaisePostureTests(unittest.TestCase):
    def test_reports_only_simple_split_raises(self) -> None:
        source = (
            "raise TypeError(\n"
            '    f"{actual_type=} ≠ {expected_type=}"\n'
            ")\n"
        )

        self.assertEqual(first=[1], second=unnecessary_raise_breaks(source=source))
        self.assertEqual(first=[], second=unnecessary_raise_breaks(source='raise TypeError("bad")\n'))

        self.assertEqual(
            first=[],
            second=unnecessary_raise_breaks(
                source=(
                    'raise Error(\n'
                    '    "bad",\n'
                    ') from cause\n'
                )
            ),
        )

        self.assertEqual(
            first=[],
            second=unnecessary_raise_breaks(
                source=(
                    'raise Error(\n'
                    '    "bad",  # reason\n'
                    ')\n'
                )
            ),
        )

    def test_authored_python_avoids_unnecessary_raise_breaks(self) -> None:
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}"
            for source_path in python_sources(production_only=False)
            for line in unnecessary_raise_breaks(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
