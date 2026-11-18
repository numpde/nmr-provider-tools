"""Put multiline comprehensions between a visibly open and closed delimiter.

The opening delimiter ends its line and the closing delimiter gets its own line.
This applies to list, set and dictionary comprehensions and to parenthesized
generator expressions, including generators used as a call's only argument.
"""

from tools.container_guard import require_container

require_container()

import ast
import io
import tokenize
import unittest

from .sources import ROOT, python_sources


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_OPENERS = {"(": ")", "[": "]", "{": "}"}


def _delimiter_pairs(*, source: str) -> dict[tuple[int, int], tokenize.TokenInfo]:
    """Map each real opening delimiter position to its matching closer."""
    stack = []
    pairs = {}
    lines = source.splitlines()

    for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=source).readline):
        if token.type != tokenize.OP:
            continue

        if token.string in _OPENERS:
            stack.append(token)
        elif token.string in _OPENERS.values() and stack and _OPENERS[stack[-1].string] == token.string:
            opening = stack.pop()
            opening_byte_column = len(lines[opening.start[0] - 1][:opening.start[1]].encode(encoding="utf-8"))
            pairs[(opening.start[0], opening_byte_column)] = token

    return pairs


def _character_column(*, line: str, byte_column: int) -> int:
    """Convert an AST UTF-8 byte offset to a tokenizer character column."""

    return len(line.encode(encoding="utf-8")[:byte_column].decode(encoding="utf-8"))


def multiline_comprehension_findings(*, source: str) -> list[tuple[int, str]]:
    """Return layout violations for multiline comprehension delimiters."""
    lines = source.splitlines()
    pairs = _delimiter_pairs(source=source)
    findings = set()

    for expression in ast.walk(node=ast.parse(source=source)):
        if not isinstance(expression, _COMPREHENSIONS):
            continue

        closing = pairs.get((expression.lineno, expression.col_offset))

        if closing is None or expression.end_lineno == expression.lineno:
            continue

        opening_line = lines[expression.lineno - 1]
        opening_column = _character_column(line=opening_line, byte_column=expression.col_offset)
        between_open_and_end = opening_line[opening_column + 1:]

        if between_open_and_end.split("#", maxsplit=1)[0].strip():
            findings.add((expression.lineno, "put the opening comprehension delimiter at the end of its line"))

        closing_line = lines[closing.start[0] - 1]
        before_closing = closing_line[:closing.start[1]]
        after_closing = closing_line[closing.end[1]:]

        if before_closing.strip() or after_closing.strip():
            findings.add((closing.start[0], "put the closing comprehension delimiter on its own line"))

    return sorted(findings)


class MultilineComprehensionPostureTests(unittest.TestCase):
    def test_multiline_collection_comprehensions_keep_their_delimiters_visible(self) -> None:
        sources = (
            "values = [\n    item for item in items\n]\n",
            "values = {\n    item for item in items\n}\n",
            "values = {\n    item.key: item.value for item in items\n}\n",
        )

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=multiline_comprehension_findings(source=source))

    def test_multiline_generator_expressions_use_their_own_or_call_delimiters(self) -> None:
        sources = (
            "values = (\n    item for item in items\n)\n",
            "consume(\n    item for item in items\n)\n",
            "consume((\n    item for item in items\n)\n)\n",
        )

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=multiline_comprehension_findings(source=source))

    def test_expression_after_opening_and_inline_closing_are_reported(self) -> None:
        source = (
            "one = {item for item in items\n}\n"
            "two = [\n    item for item in items]\n"
            "three = {item.key: item.value\n    for item in items}\n"
            "four = (item for item in items\n)\n"
        )

        self.assertEqual(
            [
                (1, "put the opening comprehension delimiter at the end of its line"),
                (4, "put the closing comprehension delimiter on its own line"),
                (5, "put the opening comprehension delimiter at the end of its line"),
                (6, "put the closing comprehension delimiter on its own line"),
                (7, "put the opening comprehension delimiter at the end of its line"),
            ],
            multiline_comprehension_findings(source=source),
        )

    def test_single_line_comprehensions_and_other_multiline_groups_are_ignored(self) -> None:
        source = (
            "values = [item for item in items]\n"
            "mapping = {key: value for key, value in pairs}\n"
            "generated = (item for item in items)\n"
            "call(\n    first,\n    second,\n)\n"
            "text = f\"{[item for item in items]}\"\n"
        )

        self.assertEqual(first=[], second=multiline_comprehension_findings(source=source))

    def test_nested_comprehensions_and_comments_keep_delimiter_ownership(self) -> None:
        source = (
            "values = [\n"
            "    {\n"
            "        item for item in group\n"
            "    }\n"
            "    for group in groups\n"
            "]\n"
            "# {not a comprehension}\n"
            "text = f\"{{'for': 'not a comprehension'}} {[item for item in items]}\"\n"
        )

        self.assertEqual(first=[], second=multiline_comprehension_findings(source=source))

    def test_all_authored_python_uses_multiline_comprehension_delimiter_layout(self) -> None:
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}: {reason}"
            for source_path in python_sources(production_only=False)
            for (line, reason) in multiline_comprehension_findings(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
