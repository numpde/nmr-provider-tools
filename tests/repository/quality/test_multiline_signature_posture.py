"""Keep multiline function parameters below an otherwise empty opening delimiter.

The closing parenthesis starts its own line; return annotations may follow it.
Calls and single-line signatures retain their existing layout rules.
"""

from tools.container_guard import require_container

require_container()

import io
import tokenize
import unittest

from .sources import ROOT, python_sources


def multiline_signature_findings(*, source: str) -> list[tuple[int, str]]:
    """Locate signature delimiters without treating defaults or strings as headers."""
    lines = source.splitlines()
    tokens = [
        token
        for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=source).readline)
        if token.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    ]
    stack = []
    closing = {}

    for index, token in enumerate(tokens):
        if token.type != tokenize.OP:
            continue

        if token.string in {"(", "[", "{"}:
            stack.append(index)
        elif token.string in {")", "]", "}"}:
            closing[stack.pop()] = index

    findings = []

    for index, token in enumerate(tokens):
        if token.type != tokenize.NAME or token.string != "def":
            continue

        # Skip the function name and any Python 3.12 type parameters before
        # locating the parameter list, whose defaults may contain nested groups
        opening_index = index + 2

        if tokens[opening_index].string == "[":
            opening_index = closing[opening_index] + 1

        opening = tokens[opening_index]
        ending = tokens[closing[opening_index]]

        if opening.start[0] == ending.start[0]:
            continue

        if tokens[opening_index + 1].start[0] == opening.start[0]:
            findings.append((opening.start[0], "start multiline signature parameters on the next line"))

        if lines[ending.start[0] - 1][:ending.start[1]].strip():
            findings.append((ending.start[0], "start the closing signature parenthesis on its own line"))

    return sorted(findings)


class MultilineSignaturePostureTests(unittest.TestCase):
    def test_parameters_on_the_opening_line_are_reported(self):
        for header in ("def f(value,", "async def f(value,", "def __init__(self,", "def f[T](value: T,"):
            with self.subTest(header=header):
                source = f"{header}\n    other=None,\n):\n    pass\n"

                self.assertEqual(first=[(1, "start multiline signature parameters on the next line")], second=multiline_signature_findings(source=source))

    def test_parameters_on_the_closing_line_are_reported(self):
        source = "def f(\n    value) -> str:\n    pass\n"

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
        source = "def f(value: str) -> tuple[\n    str,\n    int,\n]:\n    pass\n"

        self.assertEqual(first=[], second=multiline_signature_findings(source=source))

    def test_calls_strings_and_multiline_default_values_keep_their_own_delimiters(self):
        source = (
            "text = 'def f(value,'\n"
            "result = call(value,\n    other)\n"
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

    def test_authored_python_uses_multiline_signature_layout(self):
        findings = [
            f"{path.relative_to(ROOT)}:{line}: {message}"
            for path in python_sources(production_only=False)
            for line, message in multiline_signature_findings(source=path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
