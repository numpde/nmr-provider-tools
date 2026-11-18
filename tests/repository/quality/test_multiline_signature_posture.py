"""Keep multiline function parameters and call arguments between their delimiters.

The closing parenthesis starts its line. Single-line argument and parameter
lists stay compact, even when the callee or return annotation spans lines.
"""

from tools.container_guard import require_container

require_container()

import ast
import io
import tokenize
import unittest

from .sources import ROOT, python_sources


def multiline_signature_findings(*, source: str) -> list[tuple[int, str]]:
    """Check parameter and argument list boundaries without counting nested delimiters."""
    lines = source.splitlines()

    tokens = [
        token
        for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=source).readline)
        if token.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    ]

    stack = []
    closing = {}

    for (index, token) in enumerate(tokens):
        if token.type != tokenize.OP:
            continue

        if token.string in {"(", "[", "{"}:
            stack.append(index)
        elif token.string in {")", "]", "}"}:
            closing[stack.pop()] = index

    lists = {}

    for (index, token) in enumerate(tokens):
        if token.type != tokenize.NAME or token.string != "def":
            continue

        # Skip the function name and any Python 3.12 type parameters before
        # locating the parameter list, whose defaults may contain nested groups
        opening_index = index + 2

        if tokens[opening_index].string == "[":
            opening_index = closing[opening_index] + 1

        lists[opening_index] = ("signature", "parameters")

    # A call's AST span ends immediately after its own closing parenthesis.
    # Match that position rather than guessing where a complex callee ends.
    opening_by_call_end = {
        (line, len(lines[line - 1][:column].encode("utf-8"))): opening_index
        for (opening_index, closing_index) in closing.items()
        if tokens[closing_index].string == ")"
        for (line, column) in (tokens[closing_index].end,)
    }

    for expression in ast.walk(ast.parse(source)):
        if isinstance(expression, ast.Call):
            opening_index = opening_by_call_end.get((expression.end_lineno, expression.end_col_offset))

            if opening_index is not None:
                lists[opening_index] = ("call", "arguments")

    findings = []

    for (opening_index, (kind, items)) in lists.items():
        opening = tokens[opening_index]
        ending = tokens[closing[opening_index]]

        if opening.start[0] == ending.start[0]:
            continue

        if tokens[opening_index + 1].start[0] == opening.start[0]:
            findings.append((opening.start[0], f"start multiline {kind} {items} on the next line"))

        if lines[ending.start[0] - 1][:ending.start[1]].strip():
            findings.append((ending.start[0], f"start the closing {kind} parenthesis on its own line"))

    return sorted(findings)


class MultilineSignaturePostureTests(unittest.TestCase):
    def test_parameters_on_the_opening_line_are_reported(self):
        for header in ("def f(value,", "async def f(value,", "def __init__(self,", "def f[T](value: T,"):
            with self.subTest(header=header):
                source = (
                    f"{header}\n"
                    f"    other=None,\n"
                    f"):\n"
                    f"    pass\n"
                )

                self.assertEqual(first=[(1, "start multiline signature parameters on the next line")], second=multiline_signature_findings(source=source))

    def test_parameters_on_the_closing_line_are_reported(self):
        source = (
            "def f(\n"
            "    value) -> str:\n"
            "    pass\n"
        )

        self.assertEqual(first=[(2, "start the closing signature parenthesis on its own line")], second=multiline_signature_findings(source=source))

    def test_multiline_parameters_and_return_annotations_are_accepted(self):
        source = (
            "@decorate(option=(1, 2))\n"
            "async def f[T: (Left, Right)](  # The schema stays visible below\n"
            "    value: T,\n"
            "    /,\n"
            "    *,\n"
            "    default=build(1, 2),\n"
            ") -> tuple[T, str]:\n"
            "    pass\n"
        )

        self.assertEqual(first=[], second=multiline_signature_findings(source=source))

    def test_single_line_parameters_are_not_made_multiline_by_the_return_annotation(self):
        source = (
            "def f(value: str) -> tuple[\n"
            "    str,\n"
            "    int,\n"
            "]:\n"
            "    pass\n"
        )

        self.assertEqual(first=[], second=multiline_signature_findings(source=source))

    def test_calls_strings_and_multiline_default_values_keep_their_own_delimiters(self):
        source = (
            "text = 'def f(value,'\n"
            "result = call(value, other)\n"
            "def f(\n"
            "    default=build(\n"
            "        'def fake(value,',\n"
            "    ),\n"
            "):\n"
            "    pass\n"
            "def g():\n"
            "    pass\n"
        )

        self.assertEqual(first=[], second=multiline_signature_findings(source=source))

    def test_multiline_call_reports_both_argument_list_boundaries(self):
        source = (
            "self.inbox = SharedDeliveryInbox(self.queue,\n"
            "                                 self.socket,\n"
            "                                 on_failure=self.on_notice)\n"
        )

        self.assertEqual(
            first=[
                (1, "start multiline call arguments on the next line"),
                (3, "start the closing call parenthesis on its own line"),
            ],
            second=multiline_signature_findings(source=source),
        )

    def test_call_delimiters_allow_nested_closers_and_unicode_arguments(self):
        source = (
            "result = (build)(\n"
            "    wrap(\n"
            "        'café',\n"
            "    ),\n"
            ").value\n"
        )

        self.assertEqual(first=[], second=multiline_signature_findings(source=source))

    def test_authored_python_uses_multiline_signature_layout(self):
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}: {message}"
            for source_path in python_sources(production_only=False)
            for (line, message) in multiline_signature_findings(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
