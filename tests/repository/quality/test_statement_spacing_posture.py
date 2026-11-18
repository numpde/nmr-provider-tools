"""Separate returns from preceding work and give multiline statements breathing room.

Adjacent statements need a blank line when either spans multiple physical lines.
Return statements also need separation from any preceding sibling statement.
Groups of direct self-attribute assignments need separation from following work.
Provisionally, other assignment groups also need separation from non-assignment
statements. This weaker rule can yield to specific grouping rules when adopted.
This syntactic grouping includes annotations and augmented assignments.
Standalone comments and their following statement form a separated unit; trailing
inline comments impose no additional spacing.
The first statement in a suite needs no leading separator. Branch clauses stay
with their owning statement; comments and decorators stay with the next one.
"""

from tools.container_guard import require_container

require_container()

import ast
import io
import textwrap
import tokenize
import unittest

from .sources import ROOT, python_sources


def _multiline(*, statement: ast.stmt) -> bool:
    # A multiline string's contents are data, not statement layout. In particular,
    # a function docstring need not be separated from its first executable line.
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        if isinstance(statement.value.value, str):
            return False

    return statement.end_lineno > _statement_start(statement=statement)


def _statement_start(*, statement: ast.stmt) -> int:
    decorators = getattr(statement, "decorator_list", [])

    return min([statement.lineno, *(decorator.lineno for decorator in decorators)])


def _attached_start(*, statement: ast.stmt, comment_columns: dict[int, int]) -> int:
    """Include genuine standalone comments at this statement's indentation."""
    start = _statement_start(statement=statement)

    while comment_columns.get(start - 1) == statement.col_offset:
        start -= 1

    return start


def _assigns_self_attributes(*, statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif (isinstance(statement, ast.AnnAssign)) and (statement.value is not None):
        targets = [statement.target]
    else:
        return False

    return all(
        isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
        for target in targets
    )


def missing_blank_lines(*, source: str) -> list[int]:
    """Return one-based insertion lines without entering strings or branch clauses."""
    tree = ast.parse(source=source)
    lines = source.splitlines()

    comment_columns = {
        token.start[0]: token.start[1]
        for token in tokenize.generate_tokens(readline=io.StringIO(source).readline)
        if (token.type == tokenize.COMMENT) and (not lines[token.start[0] - 1][:token.start[1]].strip())
    }

    missing = set()
    assignment_types = (ast.Assign, ast.AnnAssign, ast.AugAssign)

    for owner in ast.walk(node=tree):
        for (_, value) in ast.iter_fields(node=owner):
            if (not isinstance(value, list)) or (not value) or (not all(isinstance(item, ast.stmt) for item in value)):
                continue

            for (previous, statement) in zip(value, value[1:]):
                ends_attribute_group = (_assigns_self_attributes(statement=previous)) and (not _assigns_self_attributes(statement=statement))
                ends_assignment_group = (isinstance(previous, assignment_types)) and (not isinstance(statement, assignment_types))
                previous_start = _attached_start(statement=previous, comment_columns=comment_columns)
                start = _attached_start(statement=statement, comment_columns=comment_columns)

                if not (
                    isinstance(statement, ast.Return) or
                    (ends_attribute_group) or
                    (ends_assignment_group) or
                    (previous_start < _statement_start(statement=previous)) or
                    (start < _statement_start(statement=statement)) or
                    _multiline(statement=previous) or
                    _multiline(statement=statement)
                ):
                    continue

                if not any(not line.strip() for line in lines[previous.end_lineno:start - 1]):
                    missing.add(start)

    return sorted(missing)


def _fixture_source(*, fixture_name: str) -> str:
    return (ROOT / "tests/repository/fixtures/quality/statement_spacing" / fixture_name).read_text(encoding="utf-8")


class StatementSpacingPostureTests(unittest.TestCase):
    def test_standalone_comment_and_statement_are_separated_as_one_unit(self):
        source = _fixture_source(fixture_name="comment_without_separators.py.fixture")

        self.assertEqual(first=[2, 4], second=missing_blank_lines(source=source))
        separated_source = _fixture_source(fixture_name="comment_with_separators.py.fixture")

        self.assertEqual(first=[], second=missing_blank_lines(source=separated_source))

    def test_commented_first_statement_needs_only_a_trailing_separator(self):
        source = _fixture_source(fixture_name="commented_first_statement.py.fixture")

        self.assertEqual(first=[4], second=missing_blank_lines(source=source))

    def test_commented_last_statement_needs_no_trailing_blank(self):
        source = _fixture_source(fixture_name="commented_last_statement.py.fixture")

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_consecutive_commented_statements_share_one_separator(self):
        source = _fixture_source(fixture_name="consecutive_commented_statements.py.fixture")

        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_inline_comments_do_not_require_separators(self):
        source = _fixture_source(fixture_name="inline_comment.py.fixture")

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_hash_inside_a_string_is_not_an_attached_comment(self):
        source = _fixture_source(fixture_name="hash_in_string.py.fixture")

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_decorator_internal_comment_stays_with_the_declaration(self):
        source = _fixture_source(fixture_name="comment_between_decorators.py.fixture")

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_consecutive_guards_require_a_separator(self):
        source = (
            "if missing:\n"
            "    raise RuntimeError()\n"
            "if pending:\n"
            "    submit()\n"
        )

        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_multiline_calls_need_space_on_both_sides(self):
        source = (
            "before()\n"
            "value = call(\n"
            "    argument=True,\n"
            ")\n"
            "after()\n"
        )

        self.assertEqual(first=[2, 5], second=missing_blank_lines(source=source))

    def test_first_statement_in_each_suite_needs_no_leading_blank(self):
        sources = [
            "value = call(\n"
            "    argument=True,\n"
            ")\n",
            "def run():\n"
            "    value = call(\n"
            "        argument=True,\n"
            "    )\n",
            "if ready:\n"
            "    call(\n"
            "        argument=True,\n"
            "    )\n"
            "else:\n"
            "    other()\n",
            "async def run():\n"
            "    async with session():\n"
            "        await send()\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_simple_statements_can_stay_grouped(self):
        source = (
            "first = 1\n"
            "second = 2\n"
            "\n"
            "write()\n"
            "flush()\n"
        )

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_assignment_group_is_separated_from_following_action(self):
        sources = [
            "tokens = consume()\n"
            "generated = generate(tokens)\n"
            "validate(generated)\n",
            "value: Value = calculate()\n"
            "validate(value)\n",
            "value: Value\n"
            "validate(value)\n",
            "count += 1\n"
            "report(count)\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[len(source.splitlines())], second=missing_blank_lines(source=source))

    def test_assignments_and_actions_each_remain_grouped(self):
        source = (
            "first = 1\n"
            "second: int = 2\n"
            "second += 1\n"
            "\n"
            "write()\n"
            "flush()\n"
        )

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_action_before_assignment_does_not_require_separation(self):
        source = (
            "prepare()\n"
            "value = calculate()\n"
        )

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_return_is_separated_from_preceding_work(self):
        sources = [
            "async def run():\n"
            "    observation = await operation.wait()\n"
            "    return observation.require_result()\n",
            "def run():\n"
            "    result = calculate()\n"
            "    return result\n",
            "def run():\n"
            "    write()\n"
            "    return\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[3], second=missing_blank_lines(source=source))

                self.assertEqual(
                    first=[],
                    second=missing_blank_lines(
                        source=source.replace(
                            (
                                "\n"
                                "    return"
                            ),
                            (
                                "\n"
                                "\n"
                                "    return"
                            ),
                        )
                    ),
                )

    def test_return_as_first_statement_stays_compact(self):
        sources = [
            "def run():\n"
            "    return result\n",
            "def run(): return result\n",
            "def run():\n"
            "    if ready:\n"
            "        return result\n"
            "    else:\n"
            "        return other\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_return_comments_stay_attached(self):
        source = (
            "def run():\n"
            "    result = calculate()\n"
            "    # Keep the exact result.\n"
            "    return result\n"
        )

        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

        self.assertEqual(
            first=[],
            second=missing_blank_lines(
                source=source.replace(
                    (
                        "\n"
                        "    #"
                    ),
                    (
                        "\n"
                        "\n"
                        "    #"
                    ),
                )
            ),
        )

    def test_return_text_in_strings_is_not_code(self):
        source = (
            'def run():\n'
            '    source = """calculate()\n'
            'return result\n'
            '"""\n'
        )

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_self_attribute_assignments_stay_grouped_before_following_work(self):
        source = (
            "def run(self):\n"
            "    self.first = first\n"
            "    self.second: Value = second\n"
            "    reservation_ref, = executor.reservations\n"
        )

        self.assertEqual(first=[4], second=missing_blank_lines(source=source))

        self.assertEqual(
            first=[],
            second=missing_blank_lines(
                source=source.replace(
                    (
                        "\n"
                        "    reservation"
                    ),
                    (
                        "\n"
                        "\n"
                        "    reservation"
                    ),
                )
            ),
        )

    def test_self_attribute_group_can_end_the_block(self):
        source = (
            "def run(self):\n"
            "    self.first = first\n"
            "    self.second = second\n"
        )

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_attribute_group_comments_stay_attached_to_following_work(self):
        source = (
            "def run(self):\n"
            "    self.first = first\n"
            "    # Obtain the reservation.\n"
            "    reservation_ref, = executor.reservations\n"
        )

        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_other_assignment_groups_remain_compact(self):
        sources = [
            "def run():\n"
            "    owner.first = first\n"
            "    owner.second = second\n"
            "    local = value\n",
            "def run(self):\n"
            "    self.value: Value\n"
            "    local = value\n",
            "def run(self):\n"
            "    local = self.value = value\n"
            "    other = value\n",
            "def run(self):\n"
            "    (self.first, self.second) = values\n"
            "    local = value\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_chained_self_attribute_assignments_end_a_group(self):
        source = (
            "def run(self):\n"
            "    self.first = self.second = value\n"
            "    local = value\n"
        )

        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_shared_semicolon_line_still_requires_a_separator(self):
        source = (
            "value = call(\n"
            "    argument=True,\n"
            "); after()\n"
        )

        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_complete_compound_statements_are_separated_from_neighbors(self):
        blocks = [
            "if ready:\n"
            "    act()",
            "for item in items:\n"
            "    act()\n"
            "else:\n"
            "    finish()",
            "while ready:\n"
            "    act()",
            "with session():\n"
            "    act()",
            "try:\n"
            "    act()\n"
            "except Error:\n"
            "    recover()\n"
            "finally:\n"
            "    finish()",
            "try:\n"
            "    act()\n"
            "except* Error:\n"
            "    recover()",
            "match value:\n"
            "    case 1:\n"
            "        act()",
            "def run():\n"
            "    act()",
            "class Owner:\n"
            "    pass",
        ]

        for block in blocks:
            with self.subTest(block=block):
                source = "before()\n" + block + (
                    "\n"
                    "after()\n"
                )

                self.assertEqual(first=[2, len(source.splitlines())], second=missing_blank_lines(source=source))

    def test_branch_and_exception_clauses_are_not_separate_statements(self):
        source = textwrap.dedent(
            '''\
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
            '''
        )

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_comments_and_multiline_decorators_stay_attached(self):
        source = (
            "before()\n"
            "# repo-check: keep-attached\n"
            "@decorate(\n"
            "    enabled=True,\n"
            ")\n"
            "def run():\n"
            "    act()\n"
        )

        self.assertEqual(first=[2], second=missing_blank_lines(source=source))

        self.assertEqual(
            first=[],
            second=missing_blank_lines(
                source=source.replace(
                    "before()\n",
                    (
                        "before()\n"
                        "\n"
                    ),
                )
            ),
        )

    def test_multiline_decorator_makes_an_inline_definition_multiline(self):
        for definition in ["def run(): pass", "async def run(): pass", "class Owner: pass"]:
            with self.subTest(definition=definition):
                source = (
                    "before()\n"
                    "@decorate(\n"
                    "    enabled=True,\n"
                    ")\n"
                ) + definition + (
                    "\n"
                    "after()\n"
                )

                self.assertEqual(first=[2, 6], second=missing_blank_lines(source=source))

    def test_comment_only_lines_do_not_replace_a_blank_separator(self):
        source = (
            "if ready:\n"
            "    act()\n"
            "# Explain the next step.\n"
            "after()\n"
        )

        self.assertEqual(first=[3], second=missing_blank_lines(source=source))

    def test_multiline_strings_and_docstrings_remain_data(self):
        source = (
            'def run():\n'
            '    """Explain.\n'
            '    More context.\n'
            '    """\n'
            '    act()\n'
        )

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

        source = (
            'value = """one\n'
            'if ready:\n'
            '    act()\n'
            'if pending:\n'
            '    other()\n'
            '"""\n'
        )

        self.assertEqual(first=[], second=missing_blank_lines(source=source))

    def test_all_authored_python_obeys_statement_spacing(self):
        findings = []

        for source_path in python_sources(production_only=False):
            for line in missing_blank_lines(source=source_path.read_text(encoding="utf-8")):
                findings.append(f"{source_path.relative_to(ROOT)}:{line}: insert a blank line here; separate commented statements, returns, completed self-attribute groups, provisional assignment-to-action boundaries and multiline statements or blocks; split a shared semicolon line first")

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
