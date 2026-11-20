"""Write direct raising guards as a negated acceptance condition.

Require one outer not. Parenthesize its complete operand unless it is a direct
call. Nested conditions own their own raises.
"""

from tools.container_guard import require_container

require_container()

import ast
import io
import tokenize
import unittest

from operations.checker.findings import assert_no_findings

from .sources import python_sources, ROOT


def _raising_guard(*, statement: ast.If) -> bool:
    # Nested conditions own their own raises. An exception name or constructor
    # does not determine whether this condition governs rejection.
    return any(isinstance(child, ast.Raise) for child in statement.body)


def _parenthesized_not(*, expression: str) -> bool:
    tokens = (
        [
            token
            for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=expression).readline)
            if token.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
        ]
    )[1:]

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
                return index == (len(tokens) - 1)

    return False


def guard_findings(*, source: str) -> list[tuple[int, str]]:
    findings = []

    for statement in ast.walk(node=ast.parse(source=source)):
        if (not isinstance(statement, ast.If)) or (not _raising_guard(statement=statement)):
            continue

        condition = statement.test

        if (not isinstance(condition, ast.UnaryOp)) or (not isinstance(condition.op, ast.Not)):
            findings.append((statement.lineno, "write the raising guard as if not (expected_condition): raise ..."))
        elif (
            (not isinstance(condition.operand, ast.Call)) and
            (not _parenthesized_not(expression=ast.get_source_segment(source=source, node=condition)))
        ):
            findings.append((statement.lineno, "parenthesize the complete expected condition after not"))

    return sorted(findings)


class ExpectedConditionGuardPostureTests(unittest.TestCase):
    def test_mixed_disjunction_in_raising_guard_is_flagged(self):
        for condition in (
            "(not tokens) or (token_count > limit)",
            "(bad_count > limit) or (not samples)",
            "(not ready) or missing or (not permitted)",
        ):
            with self.subTest(condition=condition):
                source = (
                    f"if {condition}:\n"
                    f"    raise ScientificRejection()\n"
                )

                self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_compound_guards_require_one_outer_negation(self):
        for condition in (
            "(not tokens) or (not (token_count <= limit))",
            "enabled and not available",
            "invalid or missing",
        ):
            with self.subTest(condition=condition):
                source = (
                    f"if {condition}:\n"
                    f"    raise ValueError()\n"
                )

                self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_mixed_disjunction_without_direct_raise_is_outside_rule(self):
        source = (
            "if (not tokens) or (token_count > limit):\n"
            "    return None\n"
        )

        self.assertEqual(first=[], second=guard_findings(source=source))

    def test_examples_require_expected_condition_form(self):
        sources = [
            "if not 1 <= count <= settings.beam_size:\n"
            "    raise ValueError\n",
            'if type(candidate) is not str:\n'
            '    raise TypeError(f"Unexpected {type(candidate)=}")\n',
            "if candidate not in supported:\n"
            "    raise ValueError()\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_direct_call_predicates_need_no_extra_grouping(self):
        for predicate in ["predicate()", "isinstance(candidate, str)", "owner.ready()", "np.isfinite(samples).all()"]:
            for condition in [f"not {predicate}", f"not ({predicate})"]:
                with self.subTest(condition=condition):
                    source = (
                        f"if {condition}:\n"
                        f"    raise builtins.TypeError() from cause\n"
                    )

                    self.assertEqual(first=[], second=guard_findings(source=source))

        source = (
            "if not predicate() == expected:\n"
            "    raise ValueError()\n"
        )

        self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_parentheses_enclose_the_complete_expected_condition(self):
        for condition in ("not (first) and second", "not (first) or second", "not (first) == second"):
            with self.subTest(condition=condition):
                source = (
                    f"if {condition}:\n"
                    f"    raise ValueError()\n"
                )

                self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_canonical_conditions_are_accepted(self):
        conditions = [
            "not (tokens and (token_count <= limit))",
            "not (1 <= count <= limit)",
            "not (type(candidate) is str)",
            "not (candidate in supported)",
            "not ((candidate in supported))",
            'not (candidate == "(")',
            "not (\n"
            "    first  # ) is comment text\n"
            "    and second\n"
            ")",
        ]

        for condition in conditions:
            with self.subTest(condition=condition):
                source = (
                    f"if {condition}:\n"
                    f"    raise ValueError()\n"
                )

                self.assertEqual(first=[], second=guard_findings(source=source))

    def test_enclosing_if_parentheses_do_not_replace_not_operand_parentheses(self):
        source = (
            "if (not ready):\n"
            "    raise ValueError()\n"
        )

        self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_any_exception_type_and_raise_form_is_in_scope(self):
        sources = [
            "if tokens is not None:\n"
            "    raise RuntimeError()\n",
            "if failure is not None:\n"
            "    raise failure\n",
            "if invalid not in allowed:\n"
            "    raise\n",
            "if type(value) is not str:\n"
            "    log()\n"
            "    raise TypeError()\n",
            "if type(value) is not str:\n"
            "    raise TypeError()\n"
            "else:\n"
            "    accept()\n",
            "if type(value) is not str:\n"
            "    raise custom.Error() from cause\n",
            "if not permitted:\n"
            "    raise BaseExceptionGroup('failed', causes)\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=1, second=len(guard_findings(source=source)))

    def test_non_raising_conditions_are_outside_the_rule(self):
        sources = [
            "if not permitted:\n"
            "    return value\n",
            "if value is not None:\n"
            "    use(value)\n",
            "if value not in allowed:\n"
            "    if not (acceptable):\n"
            "        raise Error()\n",
        ]

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(first=[], second=guard_findings(source=source))

    def test_positive_guards_are_reported_without_suggesting_an_inversion(self):
        for condition in ["left != right", "value < bound", "value > bound", "first is not second is not third", "invalid or missing"]:
            source = (
                f"if {condition}:\n"
                f"    raise ValueError()\n"
            )

            self.assertEqual(first=1, second=len(guard_findings(source=source)))

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

    def test_all_authored_raising_guards_use_expected_conditions(self):
        findings = []

        for source_path in python_sources(production_only=False):
            for (line, reason) in guard_findings(source=source_path.read_text(encoding="utf-8")):
                findings.append(f"{source_path.relative_to(ROOT)}:{line}: {reason}")

        assert_no_findings(findings)
