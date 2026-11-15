"""Spell negative raising guards as ``if not (expected_condition): raise ...``.

This standalone spelling check covers conditions whose body directly raises:
an outer not needs encompassing parentheses, and single is-not/not-in comparisons
should negate their accepted identity/membership condition. It does not infer
accepted predicates for arbitrary equality, ordering or boolean expressions.
Exception type, exception construction and chaining do not restrict the rule.
The existing validation checker reviews range and value logic independently.
"""

from tools.container_guard import require_container

require_container()

import ast
import io
import tokenize
import unittest

from .sources import python_sources, ROOT


def _raising_guard(*, statement: ast.If) -> bool:
    # Nested conditions own their own raises. An exception name or constructor
    # does not determine whether this condition governs rejection.
    return any(isinstance(child, ast.Raise) for child in statement.body)


def _parenthesized_not(*, expression: str) -> bool:
    tokens = [
        token
        for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=expression).readline)
        if token.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    ][1:]

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


def guard_findings(*, source: str) -> list[tuple[int, str]]:
    findings = []

    for statement in ast.walk(node=ast.parse(source=source)):
        if not isinstance(statement, ast.If) or not _raising_guard(statement=statement):
            continue

        condition = statement.test

        if isinstance(condition, ast.UnaryOp) and isinstance(condition.op, ast.Not):
            if not _parenthesized_not(expression=ast.get_source_segment(source=source, node=condition)):
                findings.append((statement.lineno, "parenthesize the complete expected condition after not"))
        elif isinstance(condition, ast.Compare) and len(condition.ops) == 1:
            if isinstance(condition.ops[0], ast.IsNot):
                findings.append((statement.lineno, "write the identity guard as if not (... is ...): raise ..."))
            elif isinstance(condition.ops[0], ast.NotIn):
                findings.append((statement.lineno, "write the membership guard as if not (... in ...): raise ..."))

    return sorted(findings)


class ExpectedConditionGuardPostureTests(unittest.TestCase):
    def test_examples_require_expected_condition_form(self):
        sources = [
            "if not 1 <= count <= settings.beam_size:\n    raise ValueError\n",
            'if type(candidate) is not str:\n    raise TypeError(f"Unexpected {type(candidate)=}")\n',
            "if candidate not in supported:\n    raise ValueError()\n",
            "if not isinstance(candidate, str):\n    raise builtins.TypeError() from cause\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_parentheses_enclose_the_complete_expected_condition(self):
        for condition in ["not (first) and second", "not (first) or second"]:
            # Boolean tests belong to the existing value-logic check, not this
            # spelling check. An actual outer not has its own AST boundary.
            source = f"if {condition}:\n    raise ValueError()\n"

            self.assertEqual(first=[], second=guard_findings(source=source))

        source = "if not (first) == second:\n    raise ValueError()\n"

        self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_canonical_conditions_are_accepted(self):
        conditions = [
            "not (1 <= count <= limit)",
            "not (type(candidate) is str)",
            "not (candidate in supported)",
            "not ((candidate in supported))",
            'not (candidate == "(")',
            "not (\n    first  # ) is comment text\n    and second\n)",
        ]

        for condition in conditions:
            with self.subTest(condition=condition):
                source = f"if {condition}:\n    raise ValueError()\n"

                self.assertEqual(first=[], second=guard_findings(source=source))

    def test_enclosing_if_parentheses_do_not_replace_not_operand_parentheses(self):
        source = "if (not ready):\n    raise ValueError()\n"

        self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_any_exception_type_and_raise_form_is_in_scope(self):
        sources = [
            "if tokens is not None:\n    raise RuntimeError()\n",
            "if failure is not None:\n    raise failure\n",
            "if invalid not in allowed:\n    raise\n",
            "if type(value) is not str:\n    log()\n    raise TypeError()\n",
            "if type(value) is not str:\n    raise TypeError()\nelse:\n    accept()\n",
            "if type(value) is not str:\n    raise custom.Error() from cause\n",
            "if not permitted:\n    raise BaseExceptionGroup('failed', causes)\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_non_raising_conditions_are_outside_the_rule(self):
        sources = [
            "if not permitted:\n    return value\n",
            "if value is not None:\n    use(value)\n",
            "if value not in allowed:\n    if failed:\n        raise Error()\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=guard_findings(source=source))

    def test_unreviewed_comparison_complements_are_not_assumed(self):
        for condition in ["left != right", "value < bound", "value > bound", "first is not second is not third", "invalid or missing"]:
            source = f"if {condition}:\n    raise ValueError()\n"

            self.assertEqual(first=[], second=guard_findings(source=source))

    def test_identity_inversion_does_not_invoke_custom_equality(self):
        class NonComparable:
            def __eq__(self, other):
                raise AssertionError("Identity guards must not invoke equality")

            def __ne__(self, other):
                raise AssertionError("Identity guards must not invoke inequality")

        subject = NonComparable()

        for comparison in (subject, NonComparable(), None):
            original = subject is not comparison
            canonical = not (subject is comparison)

            self.assertIs(expr1=original, expr2=canonical)

    def test_membership_inversion_preserves_calls_and_truth_conversion(self):
        observed_calls = []

        class MembershipTruth:
            def __bool__(self):
                observed_calls.append("truth")

                return present

        class Container:
            def __contains__(self, item):
                observed_calls.append(("contains", item))

                return MembershipTruth()

        container = Container()
        subject = object()

        for present in (False, True):
            observed_calls.clear()
            original = subject not in container
            original_calls = list(observed_calls)

            observed_calls.clear()
            canonical = not (subject in container)

            self.assertIs(expr1=original, expr2=canonical)
            self.assertEqual(first=original_calls, second=observed_calls)
            self.assertEqual(first=[("contains", subject), "truth"], second=observed_calls)

    def test_all_authored_negative_raising_guards_use_expected_conditions(self):
        findings = []

        for path in python_sources(production_only=False):
            for (line, reason) in guard_findings(source=path.read_text(encoding="utf-8")):
                findings.append(f"{path.relative_to(ROOT)}:{line}: {reason}")

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
