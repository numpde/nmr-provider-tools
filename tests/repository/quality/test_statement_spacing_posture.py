"""Give multiline statements, including complete control-flow blocks, breathing room.

Adjacent statements need a blank line when either spans multiple physical lines.
The first statement in a suite needs no leading separator. Branch clauses stay
with their owning statement; comments and decorators stay with the next one.
"""

from tools.container_guard import require_container

require_container()

import ast
import textwrap
import unittest

from .sources import ROOT, python_sources


def _multiline(*, statement: ast.stmt) -> bool:
    # A multiline string's contents are data, not statement layout. In particular,
    # a function docstring need not be separated from its first executable line.
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        if isinstance(statement.value.value, str):
            return False

    decorators = getattr(statement, "decorator_list", [])
    start = min([statement.lineno, *(decorator.lineno for decorator in decorators)])
    return statement.end_lineno > start


def _attached_start(*, statement: ast.stmt, previous: ast.stmt, lines: list[str]) -> int:
    decorators = getattr(statement, "decorator_list", [])
    start = min([statement.lineno, *(decorator.lineno for decorator in decorators)])

    while start > previous.end_lineno + 1:
        preceding = lines[start - 2]
        indentation = len(preceding) - len(preceding.lstrip())

        if not preceding.lstrip().startswith("#") or indentation != statement.col_offset:
            break

        start -= 1

    return start


def missing_blank_lines(*, source: str) -> list[int]:
    """Return one-based insertion lines without entering strings or branch clauses."""
    tree = ast.parse(source=source)
    lines = source.splitlines()
    missing = set()

    for owner in ast.walk(node=tree):
        for _, value in ast.iter_fields(node=owner):
            if not isinstance(value, list) or not value or not all(isinstance(item, ast.stmt) for item in value):
                continue

            for previous, statement in zip(value, value[1:]):
                if not (_multiline(statement=previous) or _multiline(statement=statement)):
                    continue

                start = _attached_start(statement=statement, previous=previous, lines=lines)

                if not any(not line.strip() for line in lines[previous.end_lineno:start - 1]):
                    missing.add(start)

    return sorted(missing)


class StatementSpacingPostureTests(unittest.TestCase):
    def test_consecutive_guards_require_a_separator(self):
        source = "if missing:\n    raise RuntimeError()\nif pending:\n    submit()\n"
        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_multiline_calls_need_space_on_both_sides(self):
        source = "before()\nvalue = call(\n    argument=True,\n)\nafter()\n"
        self.assertEqual(first=[2, 5], second=missing_blank_lines(source=source))

    def test_first_statement_in_each_suite_needs_no_leading_blank(self):
        sources = [
            "value = call(\n    argument=True,\n)\n",
            "def run():\n    value = call(\n        argument=True,\n    )\n",
            "if ready:\n    call(\n        argument=True,\n    )\nelse:\n    other()\n",
            "async def run():\n    async with session():\n        await send()\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_simple_statements_can_stay_grouped(self):
        source = "first = 1\nsecond = 2\nwrite()\nflush()\n"
        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_shared_semicolon_line_still_requires_a_separator(self):
        source = "value = call(\n    argument=True,\n); after()\n"
        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_complete_compound_statements_are_separated_from_neighbors(self):
        blocks = [
            "if ready:\n    act()",
            "for item in items:\n    act()\nelse:\n    finish()",
            "while ready:\n    act()",
            "with session():\n    act()",
            "try:\n    act()\nexcept Error:\n    recover()\nfinally:\n    finish()",
            "try:\n    act()\nexcept* Error:\n    recover()",
            "match value:\n    case 1:\n        act()",
            "def run():\n    act()",
            "class Owner:\n    pass",
        ]

        for block in blocks:
            with self.subTest(block=block):
                source = "before()\n" + block + "\nafter()\n"
                self.assertEqual(first=[2, len(source.splitlines())], second=missing_blank_lines(source=source))

    def test_branch_and_exception_clauses_are_not_separate_statements(self):
        source = textwrap.dedent('''\
            if first:
                act()
            elif second:
                other()
            else:
                finish()

            try:
                act()
            except Error:
                recover()
            else:
                accept()
            finally:
                finish()
            ''')

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_comments_and_multiline_decorators_stay_attached(self):
        source = "before()\n# repo-check: keep-attached\n@decorate(\n    enabled=True,\n)\ndef run():\n    act()\n"
        self.assertEqual(first=[2], second=missing_blank_lines(source=source))
        self.assertEqual(first=[], second=missing_blank_lines(source=source.replace("before()\n", "before()\n\n")))

    def test_multiline_decorator_makes_an_inline_definition_multiline(self):
        for definition in ["def run(): pass", "async def run(): pass", "class Owner: pass"]:
            with self.subTest(definition=definition):
                source = "before()\n@decorate(\n    enabled=True,\n)\n" + definition + "\nafter()\n"
                self.assertEqual(first=[2, 6], second=missing_blank_lines(source=source))

    def test_comment_only_lines_do_not_replace_a_blank_separator(self):
        source = "if ready:\n    act()\n# Explain the next step.\nafter()\n"
        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_multiline_strings_and_docstrings_remain_data(self):
        source = 'def run():\n    """Explain.\n    More context.\n    """\n    act()\n'
        self.assertEqual(first=[], second=missing_blank_lines(source=source))
        source = 'value = """one\nif ready:\n    act()\nif pending:\n    other()\n"""\n'
        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_all_authored_python_separates_multiline_statements(self):
        findings = []

        for path in python_sources(production_only=False):
            for line in missing_blank_lines(source=path.read_text(encoding="utf-8")):
                findings.append(f"{path.relative_to(ROOT)}:{line}: separate sibling statements around this multiline statement or block with a blank line; split a shared semicolon line first")

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
