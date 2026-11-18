"""Keep adjacent items on separate lines inside multiline brackets.

The rule covers calls, signatures, literals, imports and subscriptions uniformly.
Single-line bracket groups stay compact. Nested groups own their own commas;
lambda parameter commas and string contents are not outer item separators.
Multiline dictionary, set, list and tuple literals also put their entries
between the opening and closing delimiters.
"""

from tools.container_guard import require_container

require_container()

from dataclasses import dataclass, field
import ast
import io
import tokenize
import unittest

from .sources import ROOT, python_sources
from .test_multiline_comprehension_posture import _delimiter_pairs


@dataclass(slots=True)
class _Frame:
    opening: tokenize.TokenInfo
    items: list[list[tokenize.TokenInfo]] = field(default_factory=list)
    current: list[tokenize.TokenInfo] = field(default_factory=list)
    lambda_headers: int = 0


def shared_item_lines(*, source: str) -> list[tuple[int, int, int]]:
    """Return each next item's line, column and continuation indent.

    Tokenizing keeps comments and string contents opaque, including Python 3.12
    f-string replacement fields. Report only item boundaries, so a caller can
    insert whitespace without editing any argument expression or literal.
    """
    lines = source.splitlines()
    stack = []
    findings = set()
    fstring_depth = 0

    for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=source).readline):
        if token.type == tokenize.FSTRING_START:
            if fstring_depth == 0 and stack:
                stack[-1].current.append(token)

            fstring_depth += 1

            continue

        if token.type == tokenize.FSTRING_END:
            fstring_depth -= 1

            if fstring_depth == 0 and stack:
                stack[-1].current.append(token)

            continue

        if fstring_depth or token.type in {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT, tokenize.ENDMARKER}:
            continue

        if token.type == tokenize.OP and token.string in {"(", "[", "{"}:
            if stack:
                stack[-1].current.append(token)

            stack.append(_Frame(opening=token))
            continue

        if token.type == tokenize.OP and token.string in {")", "]", "}"}:
            frame = stack.pop()

            if frame.current:
                frame.items.append(frame.current)

            if frame.opening.start[0] != token.end[0]:
                opening_line = lines[frame.opening.start[0] - 1]
                indent = len(opening_line) - len(opening_line.lstrip()) + 4

                for item in frame.items:
                    first = item[0]

                    if not lines[first.start[0] - 1][:first.start[1]].strip():
                        indent = first.start[1]

                        break

                for (previous, following) in zip(frame.items, frame.items[1:]):
                    if previous[-1].end[0] == following[0].start[0]:
                        findings.add((*following[0].start, indent))

            if stack:
                stack[-1].current.append(token)

            continue

        if not stack:
            continue

        frame = stack[-1]

        if token.type == tokenize.NAME and token.string == "lambda":
            frame.lambda_headers += 1
        elif token.type == tokenize.OP and token.string == ":" and frame.lambda_headers:
            frame.lambda_headers -= 1
        elif token.type == tokenize.OP and token.string == "," and not frame.lambda_headers:
            if frame.current:
                frame.items.append(frame.current)
                frame.current = []

            continue

        frame.current.append(token)

    return sorted(findings)


def multiline_literal_findings(*, source: str) -> list[tuple[int, str]]:
    """Check delimiters of multiline literals, including dictionary unpacking.

    AST columns count UTF-8 bytes, so inspect encoded lines when locating the
    delimiters. A tuple may have no parentheses of its own, so require its
    opening token to match the closing token at the end of its AST span.
    """
    lines = source.encode(encoding="utf-8").splitlines()
    pairs = _delimiter_pairs(source=source)
    findings = []

    for expression in ast.walk(node=ast.parse(source=source)):
        if not isinstance(expression, (ast.Dict, ast.Set, ast.List, ast.Tuple)) or expression.lineno == expression.end_lineno:
            continue

        opening_line = lines[expression.lineno - 1]
        opening_column = expression.col_offset
        opening_symbol = opening_line[opening_column:opening_column + 1]
        if isinstance(expression, ast.Tuple):
            closing = pairs.get((expression.lineno, opening_column))

            if opening_symbol != b"(" or closing is None or closing.end[0] != expression.end_lineno:
                continue

            end_line = lines[expression.end_lineno - 1].decode("utf-8")

            if len(end_line[:closing.end[1]].encode("utf-8")) != expression.end_col_offset:
                continue

        delimiter = {b"{": "brace", b"[": "bracket", b"(": "parenthesis"}[opening_symbol]
        after_opening = opening_line[opening_column + 1:]

        if after_opening.split(b"#", maxsplit=1)[0].strip():
            findings.append((expression.lineno, f"put literal entries below the opening {delimiter}"))

        before_closing = lines[expression.end_lineno - 1][:expression.end_col_offset - 1]

        if before_closing.strip():
            findings.append((expression.end_lineno, f"start the closing literal {delimiter} on its own line"))

    return sorted(findings)


class MultilineBracketPostureTests(unittest.TestCase):
    def test_multiline_dictionary_unpacking_cannot_share_brace_lines(self):
        source = (
            'notice = {**facts("delivery_published"),\n'
            '          "replayed": receipt.replayed,\n'
            '          "recovered": retried or alerted}\n'
        )

        self.assertEqual(
            first=[
                (1, "put literal entries below the opening brace"),
                (3, "start the closing literal brace on its own line"),
            ],
            second=multiline_literal_findings(source=source),
        )

    def test_returned_multiline_dict_flags_outer_braces_only(self):
        source = (
            "def status():\n"
            '    return {"queue": overview,\n'
            '            "limits": {"total_items": limit}}\n'
        )

        self.assertEqual(
            first=[
                (2, "put literal entries below the opening brace"),
                (3, "start the closing literal brace on its own line"),
            ],
            second=multiline_literal_findings(source=source),
        )

    def test_multiline_braces_allow_comments_and_surrounding_call_delimiters(self):
        source = (
            'publish({  # Retain the request identity\n'
            '    **facts("delivery_published"),\n'
            '    "replayed": receipt.replayed,\n'
            '})\n'
            'values = {\n'
            '    first,\n'
            '    second,\n'
            '}\n'
            'compact = {"key": "{not a literal}"}\n'
        )

        self.assertEqual(first=[], second=multiline_literal_findings(source=source))

    def test_multiline_set_and_nested_dictionary_braces_use_utf8_positions(self):
        source = 'café = {first,\n    second}\nnested = {\n    "clé": {"value": 1,\n        "other": 2},\n}\n'

        self.assertEqual(first=[1, 2, 4, 5], second=[line for (line, _) in multiline_literal_findings(source=source)])

    def test_multiline_list_and_parenthesized_tuple_report_both_delimiters(self):
        source = 'items = [first,\n    second]\nvalues = (first,\n    "café")\n'

        self.assertEqual(
            first=[
                (1, "put literal entries below the opening bracket"),
                (2, "start the closing literal bracket on its own line"),
                (3, "put literal entries below the opening parenthesis"),
                (4, "start the closing literal parenthesis on its own line"),
            ],
            second=multiline_literal_findings(source=source),
        )

    def test_multiline_list_and_tuple_allow_clear_delimiters(self):
        source = "items = [\n    first,\n    second,\n]\nvalues = (\n    first,\n    second,\n)\na[(first),\n  (second)]\n"

        self.assertEqual(first=[], second=multiline_literal_findings(source=source))

    def test_authored_multiline_literals_keep_entries_between_delimiters(self):
        findings = [
            f"{source_path.relative_to(ROOT)}:{line}: {message}"
            for source_path in python_sources(production_only=False)
            for (line, message) in multiline_literal_findings(source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))

    def test_shared_call_arguments_are_flagged(self):
        source = (
            "with open_checkpoint(\n"
            "    path=path, expected_sha256=digest,\n"
            ") as checkpoint:\n"
            "    load()\n"
        )

        self.assertEqual(first=[(2, 15, 4)], second=shared_item_lines(source=source))

    def test_single_line_groups_stay_compact(self):
        source = (
            "value = call(first, second, option=True)\n"
            "values = [first, second]\n"
            "pair = (first, second)\n"
        )

        self.assertEqual(first=[], second=shared_item_lines(source=source))

    def test_separate_items_and_trailing_commas_are_valid(self):
        source = (
            "value = call(\n"
            "    first,\n"
            "    option=True,\n"
            ")\n"
        )

        self.assertEqual(first=[], second=shared_item_lines(source=source))

    def test_delimiters_can_share_a_line_with_their_single_item(self):
        source = (
            "value = call(first,\n"
            "    second)\n"
        )

        self.assertEqual(first=[], second=shared_item_lines(source=source))

    def test_signatures_include_positional_and_keyword_markers(self):
        source = (
            "def run(\n"
            "    self, first, /, *, option=True,\n"
            "):\n"
            "    pass\n"
        )

        self.assertEqual(first=4, second=len(shared_item_lines(source=source)))

    def test_literals_imports_and_subscriptions_share_the_rule(self):
        sources = [
            (
                "value = [\n"
                "    first, second,\n"
                "]"
            ),
            (
                "value = (\n"
                "    first, second,\n"
                ")"
            ),
            (
                "value = {\n"
                "    first, second,\n"
                "}"
            ),
            (
                "value = {\n"
                '    "first": 1, "second": 2,\n'
                "}"
            ),
            (
                "from package import (\n"
                "    first, second,\n"
                ")"
            ),
            (
                "value = array[\n"
                "    :, :,\n"
                "]"
            ),
            (
                "value = tuple[\n"
                "    int, str,\n"
                "]"
            ),
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(shared_item_lines(source=source)))

    def test_nested_single_line_groups_own_their_commas(self):
        source = "value = call(\n    pair=(first, second),\n    mapping={first: second, third: fourth},\n)\n"

        self.assertEqual(first=[], second=shared_item_lines(source=source))

    def test_path_patterns_inside_a_multiline_keyword_tuple_need_separate_lines(self):
        source = (
            "source_files(\n"
            "    root=root,\n"
            "    patterns=(\n"
            '        "packages/**/*.py", "operations/**/*.py",\n'
            '        "tests/**/*.py",\n'
            "    ),\n"
            ")\n"
        )
        separated = source.replace(', "operations/', ',\n        "operations/')

        self.assertEqual(first=[(4, 28, 8)], second=shared_item_lines(source=source))
        self.assertEqual(first=[], second=shared_item_lines(source=separated))

    def test_nested_multiline_group_and_outer_arguments_are_both_checked(self):
        source = "value = call(\n    pair=(\n        first, second,\n    ), option=True,\n)\n"

        self.assertEqual(first=[(3, 15, 8), (4, 7, 4)], second=shared_item_lines(source=source))

    def test_lambda_parameter_commas_are_not_call_argument_boundaries(self):
        source = "value = call(\n    lambda first, second: first, option=True,\n)\n"

        self.assertEqual(first=1, second=len(shared_item_lines(source=source)))

    def test_nested_lambda_headers_have_independent_colons(self):
        source = "value = call(\n    lambda first=lambda second, third: second: first, option=True,\n)\n"

        self.assertEqual(first=1, second=len(shared_item_lines(source=source)))

    def test_string_and_comment_commas_are_opaque(self):
        source = 'value = call(\n    "first, second",  # third, fourth\n    option=True,\n)\n'

        self.assertEqual(first=[], second=shared_item_lines(source=source))

    def test_fstrings_do_not_create_bracket_frames(self):
        sources = [
            'value = call(\n    f"{first, second}", option=True,\n)\n',
            'value = call(\n    f"{f\'{mapping["key"]}\'}", option=True,\n)\n',
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(shared_item_lines(source=source)))

    def test_multiline_item_cannot_share_its_closing_line_with_next_item(self):
        source = 'value = call(\n    """first\nsecond""", option=True,\n)\n'

        self.assertEqual(first=1, second=len(shared_item_lines(source=source)))

    def test_all_authored_python_separates_multiline_bracket_items(self):
        findings = []

        for source_path in python_sources(production_only=False):
            for (line, column, _) in shared_item_lines(source=source_path.read_text(encoding="utf-8")):
                findings.append(f"{source_path.relative_to(ROOT)}:{line}:{column + 1}: put each item on a separate line inside multiline brackets")

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
