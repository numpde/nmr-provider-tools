"""Put multiline comprehensions between a visibly open and closed delimiter.

The opening delimiter ends its line and the closing delimiter gets its own line
with an optional trailing comma.
This applies to list, set and dictionary comprehensions and to parenthesized
generator expressions, including generators used as a call's only argument.
Put the filter keyword on its own line when an and/or condition spans lines.
"""

from tools.container_guard import require_container

require_container()

import ast
import io
import tokenize
import unittest

from .sources import ROOT, python_sources
from .expression_grouping import character_column, delimiter_pairs


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def multiline_comprehension_findings(*, source: str) -> list[tuple[int, str]]:
    """Return layout violations for multiline comprehension delimiters."""
    lines = source.splitlines()
    pairs = delimiter_pairs(source=source)
    findings = set()

    for expression in ast.walk(node=ast.parse(source=source)):
        if not isinstance(expression, _COMPREHENSIONS):
            continue

        closing = pairs.get((expression.lineno, expression.col_offset))

        if (closing is None) or (expression.end_lineno == expression.lineno):
            continue

        opening_line = lines[expression.lineno - 1]
        opening_column = character_column(line=opening_line, byte_column=expression.col_offset)
        between_open_and_end = opening_line[opening_column + 1:]

        if between_open_and_end.split("#", maxsplit=1)[0].strip():
            findings.add((expression.lineno, "put the opening comprehension delimiter at the end of its line"))

        closing_line = lines[closing.start[0] - 1]
        before_closing = closing_line[:closing.start[1]]
        after_closing = closing_line[closing.end[1]:]

        if (before_closing.strip()) or (after_closing.strip() not in {"", ","}):
            findings.add((closing.start[0], "put the closing comprehension delimiter on its own line"))

    return sorted(findings)


def multiline_filter_findings(*, source: str) -> list[tuple[int, str]]:
    """Find comprehension filters that start a multiline Boolean condition beside if."""
    lines = source.splitlines()

    tokens = [
        token
        for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=source).readline)
        if token.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE}
    ]

    token_indices = {token.start: index for (index, token) in enumerate(tokens)}
    findings = []

    for clause in ast.walk(node=ast.parse(source=source)):
        if not isinstance(clause, ast.comprehension):
            continue

        for condition in clause.ifs:
            if (not isinstance(condition, ast.BoolOp)) or (condition.lineno == condition.end_lineno):
                continue

            column = character_column(line=lines[condition.lineno - 1], byte_column=condition.col_offset)
            condition_start = token_indices[(condition.lineno, column)]

            # AST offsets omit grouping parentheses before the first operand
            while tokens[condition_start - 1].string == "(":
                condition_start -= 1

            keyword = tokens[condition_start - 1]
            (keyword_line, keyword_column) = keyword.start

            if lines[keyword_line - 1][:keyword_column].strip() or (keyword_line == tokens[condition_start].start[0]):
                findings.append((keyword_line, "put if on its own line before a multiline comprehension filter"))

    return sorted(findings)


class MultilineComprehensionPostureTests(unittest.TestCase):
    def test_multiline_boolean_filter_starts_after_if(self) -> None:
        source = (
            "consume(\n"
            "    (\n"
            "        model for model in models\n"
            "        if\n"
            "        (model != excluded) and\n"
            "        not ready(model)\n"
            "    ),\n"
            ")\n"
        )

        self.assertEqual(first=[], second=multiline_comprehension_findings(source=source))
        self.assertEqual(first=[], second=multiline_filter_findings(source=source))

        self.assertEqual(
            first=[(4, "put if on its own line before a multiline comprehension filter")],
            second=multiline_filter_findings(
                source=source.replace(
                    (
                        "if\n"
                        "        ("
                    ),
                    "if (",
                )
            ),
        )

        self.assertEqual(
            first=[(3, "put if on its own line before a multiline comprehension filter")],
            second=multiline_filter_findings(
                source=source.replace(
                    (
                        "models\n"
                        "        if"
                    ),
                    "models if",
                )
            ),
        )

    def test_inline_filters_and_multiline_call_filters_can_start_beside_if(self) -> None:
        source = (
            "models = [\n"
            "    model for model in models\n"
            "    if ready(model) and eligible(model)\n"
            "    if accepts(\n"
            "        model,\n"
            "    )\n"
            "]\n"
        )

        self.assertEqual(first=[], second=multiline_filter_findings(source=source))

    def test_grouped_filter_reports_its_own_keyword(self) -> None:
        source = (
            "models = {\n"
            "    model for model in models if eligible(model)\n"
            "    if (\n"
            "        (model != excluded) or\n"
            "        ready(model)\n"
            "    )\n"
            "}\n"
        )

        self.assertEqual(
            first=[(3, "put if on its own line before a multiline comprehension filter")],
            second=multiline_filter_findings(source=source),
        )

    def test_all_authored_multiline_filters_start_after_if(self) -> None:
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}: {reason}"
            for source_path in python_sources(production_only=False)
            for (line, reason) in multiline_filter_findings(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))

    def test_multiline_collection_comprehensions_keep_their_delimiters_visible(self) -> None:
        sources = (
            "values = [\n"
            "    item for item in items\n"
            "]\n",
            "values = {\n"
            "    item for item in items\n"
            "}\n",
            "values = {\n"
            "    item.key: item.value for item in items\n"
            "}\n",
        )

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=multiline_comprehension_findings(source=source))

    def test_multiline_generator_expressions_use_their_own_or_call_delimiters(self) -> None:
        sources = (
            "values = (\n"
            "    item for item in items\n"
            ")\n",
            "consume(\n"
            "    item for item in items\n"
            ")\n",
            "consume((\n"
            "    item for item in items\n"
            ")\n"
            ")\n",
        )

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=multiline_comprehension_findings(source=source))

    def test_expression_after_opening_and_inline_closing_are_reported(self) -> None:
        source = (
            "one = {item for item in items\n"
            "}\n"
            "two = [\n"
            "    item for item in items]\n"
            "three = {item.key: item.value\n"
            "    for item in items}\n"
            "four = (item for item in items\n"
            ")\n"
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
            "call(\n"
            "    first,\n"
            "    second,\n"
            ")\n"
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
