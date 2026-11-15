"""Require parentheses around tuple values and unpacking targets.

Subscription argument lists already have square brackets. Python also represents
them as AST tuples, but slice syntax such as array[:, :] cannot be parenthesized.
Nested tuple values inside those argument lists still require parentheses.
"""

from tools.container_guard import require_container

require_container()

import ast
import io
import tokenize
import unittest

from .sources import ROOT, python_sources


def _has_outer_parentheses(*, expression: str) -> bool:
    tokens = [
        token
        for token in tokenize.generate_tokens(readline=io.StringIO(expression).readline)
        if token.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    ]

    if tokens[0].string != "(":
        return False

    depth = 0

    for (index, token) in enumerate(tokens):
        if token.type != tokenize.OP:
            continue

        if token.string == "(":
            depth += 1
        elif token.string == ")":
            depth -= 1

            if depth == 0:
                return index == len(tokens) - 1

    return False


def bare_tuples(*, source: str) -> list[ast.Tuple]:
    """Find tuple syntax without its own parentheses, excluding subscription lists."""
    tree = ast.parse(source=source)
    subscription_lists = {id(node.slice) for node in ast.walk(node=tree) if isinstance(node, ast.Subscript)}

    return [
        node
        for node in ast.walk(node=tree)
        if isinstance(node, ast.Tuple)
        and id(node) not in subscription_lists
        and not _has_outer_parentheses(expression=ast.get_source_segment(source=source, node=node))
    ]


class ExplicitTuplePostureTests(unittest.TestCase):
    def test_values_and_unpacking_targets_need_parentheses(self):
        sources = [
            "value = first, second",
            "value = first,",
            "first, second = values",
            "first, = values",
            "return first, second",
            "yield first, second",
            "for first, second in values: pass",
            "values = [first for first, second in pairs]",
            "value = lambda: first, second",
            "first, *rest = values",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(bare_tuples(source=source)))

    def test_explicit_tuples_remain_valid(self):
        sources = [
            "value = (first, second)",
            "value = (first,)",
            "(first, second) = values",
            "(first,) = values",
            "return (first, second)",
            "yield (first, second)",
            "for (first, second) in values: pass",
            "values = [first for (first, second) in pairs]",
            "value = lambda: (first, second)",
            "(first, *rest) = values",
            "value = ()",
            "value = (\n    first,\n    second,\n)",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=bare_tuples(source=source))

    def test_parenthesized_elements_do_not_parenthesize_the_tuple(self):
        for source in ["value = (first), (second)", "return (first), (second)"]:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(bare_tuples(source=source)))

    def test_nested_tuples_are_checked_independently(self):
        source = "value = (first, second), third"
        (finding,) = bare_tuples(source=source)

        self.assertEqual(first="(first, second), third", second=ast.get_source_segment(source=source, node=finding))

    def test_subscription_lists_keep_python_syntax(self):
        for source in ["value: tuple[int, str]", "value = array[:, :]", "value = array[row, column]", "value = array[(row, column), :]"]:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=bare_tuples(source=source))

    def test_strings_and_comments_do_not_affect_parenthesis_matching(self):
        sources = [
            'value = ("(", ")")',
            'value = (\n    first,  # )\n    second,\n)',
            'value = "first, second"',
            '# first, second\nvalue = first',
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=bare_tuples(source=source))

    def test_non_tuple_commas_are_not_tuple_syntax(self):
        for source in ["call(first, second)", "from package import first, second", "def run(first, second): pass", "values = [first, second]"]:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=bare_tuples(source=source))

    def test_all_authored_python_uses_explicit_tuples(self):
        findings = []

        for path in python_sources(production_only=False):
            for node in bare_tuples(source=path.read_text(encoding="utf-8")):
                findings.append(f"{path.relative_to(ROOT)}:{node.lineno}: parenthesize this tuple value or unpacking target explicitly")

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
