"""Keep adjacent items on separate lines inside multiline brackets.

The rule covers calls, signatures, literals, imports and subscriptions uniformly.
Single-line bracket groups stay compact. Nested groups own their own commas;
lambda parameter commas and string contents are not outer item separators.
"""

from tools.container_guard import require_container

require_container()

from dataclasses import dataclass, field
import io
import tokenize
import unittest

from .sources import ROOT, python_sources


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


class MultilineBracketPostureTests(unittest.TestCase):
    def test_shared_call_arguments_are_flagged(self):
        source = "with open_checkpoint(\n    path=path, expected_sha256=digest,\n) as checkpoint:\n    load()\n"

        self.assertEqual(first=[(2, 15, 4)], second=shared_item_lines(source=source))

    def test_single_line_groups_stay_compact(self):
        source = "value = call(first, second, option=True)\nvalues = [first, second]\npair = (first, second)\n"

        self.assertEqual(first=[], second=shared_item_lines(source=source))

    def test_separate_items_and_trailing_commas_are_valid(self):
        source = "value = call(\n    first,\n    option=True,\n)\n"

        self.assertEqual(first=[], second=shared_item_lines(source=source))

    def test_delimiters_can_share_a_line_with_their_single_item(self):
        source = "value = call(first,\n    second)\n"

        self.assertEqual(first=[], second=shared_item_lines(source=source))

    def test_signatures_include_positional_and_keyword_markers(self):
        source = "def run(\n    self, first, /, *, option=True,\n):\n    pass\n"

        self.assertEqual(first=4, second=len(shared_item_lines(source=source)))

    def test_literals_imports_and_subscriptions_share_the_rule(self):
        sources = [
            "value = [\n    first, second,\n]",
            "value = (\n    first, second,\n)",
            "value = {\n    first, second,\n}",
            'value = {\n    "first": 1, "second": 2,\n}',
            "from package import (\n    first, second,\n)",
            "value = array[\n    :, :,\n]",
            "value = tuple[\n    int, str,\n]",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(shared_item_lines(source=source)))

    def test_nested_single_line_groups_own_their_commas(self):
        source = "value = call(\n    pair=(first, second),\n    mapping={first: second, third: fourth},\n)\n"

        self.assertEqual(first=[], second=shared_item_lines(source=source))

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

        for path in python_sources(production_only=False):
            for (line, column, _) in shared_item_lines(source=path.read_text(encoding="utf-8")):
                findings.append(f"{path.relative_to(ROOT)}:{line}:{column + 1}: put each item on a separate line inside multiline brackets")

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
