"""Require checked values and expectations in direct type-guard failures.

This shares the type-rejection scanner's syntactic scope: builtin predicates,
negative comparisons and their recognized Boolean inverses, with direct raises.
Aliases, shadowed builtins, helper predicates and message variables need review.
Direct subclass guards report the class itself and its checked parent, with no
prescribed binding names or prose. Their scanner covers single-raise bodies so
intervening assignments cannot change the evidence before it is rendered.
"""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import unittest
import pathlib
from dataclasses import dataclass

from .sources import REPOSITORY_ROOT, python_sources
from .fstrings import is_debug_expression as _is_debug_expression
from .test_type_error_posture import _builtin, _type_call, _type_rejection_operands
from .type_checks import diagnostic_subject, has_type_rejection_alternatives, rejected_type_predicates, unwrap_binding


@dataclass(frozen=True, slots=True, kw_only=True)
class _Finding:
    source_path: pathlib.Path
    line: int
    missing_subjects: tuple[str, ...]

    def diagnostic(self) -> str:
        return (
            f"{self.source_path}:{self.line}: type rejection lacks actual type evidence "
            f"for {self.missing_subjects!r}; include each rejected subject with "
            f'debug f-string syntax in natural prose, such as f"Unexpected {{type(value)=}}; expected str"'
        )


def _subject_key(*, expression: ast.expr) -> str:
    if isinstance(expression, ast.NamedExpr):
        expression = expression.target

    if isinstance(expression, ast.Name):
        expression = ast.Name(id=expression.id, ctx=ast.Load())

    return ast.dump(node=expression, include_attributes=False)


def _message_type_subjects(*, source: str, message: ast.expr, type_aliases: dict[str, ast.expr]) -> set[str]:
    if not isinstance(message, ast.JoinedStr):
        return set()

    # Only rendered message fields count. A field inside a format specification
    # controls formatting rather than presenting the rejected type to the reader.
    subjects = set()

    for value in message.values:
        if not isinstance(value, ast.FormattedValue) or not _is_debug_expression(source=source, formatted=value):
            continue

        expression = value.value

        if isinstance(expression, ast.Name) and expression.id in type_aliases:
            expression = type_aliases[expression.id]

        if _type_call(expression=expression):
            subjects.add(_subject_key(expression=expression.args[0]))

    return subjects


def _findings(*, source_path: pathlib.Path, source: str) -> list[_Finding]:
    findings = []

    for statement in ast.walk(node=ast.parse(source=source, filename=str(source_path))):
        if not isinstance(statement, ast.If):
            continue

        subjects = _type_rejection_operands(expression=statement.test, rejecting=True)
        predicates = rejected_type_predicates(expression=statement.test)

        subjects.extend(predicate.subject for predicate in predicates)

        type_aliases = {
            node.target.id: node.value
            for node in ast.walk(node=statement.test)
            if isinstance(node, ast.NamedExpr) and _type_call(expression=node.value)
        }

        for predicate in predicates:
            expected = unwrap_binding(expression=predicate.expected)

            if _type_call(expression=expected):
                subjects.append(expected.args[0])

        if not subjects:
            continue

        for child in statement.body:
            if not isinstance(child, ast.Raise) or child.exc is None:
                continue

            exception = child.exc.func if isinstance(child.exc, ast.Call) else child.exc

            if not _builtin(expression=exception, name="TypeError"):
                continue

            evidenced = (
                _message_type_subjects(source=source, message=child.exc.args[0], type_aliases=type_aliases)
                if isinstance(child.exc, ast.Call) and child.exc.args
                else set()
            )

            missing = tuple(
                dict.fromkeys(
                    ast.unparse(ast_obj=subject)
                    for subject in subjects
                    if _subject_key(expression=subject) not in evidenced
                )
            )

            if missing:
                findings.append(_Finding(source_path=source_path, line=child.lineno, missing_subjects=missing))

    return sorted(findings, key=lambda finding: finding.line)


def _expectation_name(*, expression: ast.expr) -> str | None:
    if isinstance(expression, ast.NamedExpr):
        expression = expression.target

    if not isinstance(expression, ast.Name):
        return None

    name = expression.id

    if name in {"expected_type", "expected_types"} or (name.startswith("expected_") and name.endswith(("_type", "_types"))):
        return name

    return None


def _has_type_pair(*, source: str, message: ast.JoinedStr, subject: ast.expr, expected_name: str, membership: bool) -> bool:
    for (index, value) in enumerate(message.values):
        if not (
            isinstance(value, ast.FormattedValue) and
            _type_call(expression=value.value) and
            (_subject_key(expression=value.value.args[0]) == _subject_key(expression=subject)) and
            _is_debug_expression(source=source, formatted=value)
        ):
            continue

        inequality = False

        for following in message.values[index + 1:]:
            if isinstance(following, ast.Constant) and isinstance(following.value, str):
                inequality = inequality or "≠" in following.value or (membership and "∉" in following.value)
            elif isinstance(following, ast.FormattedValue):
                if inequality and isinstance(following.value, ast.Name) and following.value.id == expected_name and _is_debug_expression(source=source, formatted=following):
                    return True

                break

    return False


def _expectation_findings(*, source_path: pathlib.Path, source: str) -> list[str]:
    findings = []

    for statement in ast.walk(node=ast.parse(source=source, filename=str(source_path))):
        if not isinstance(statement, ast.If):
            continue

        predicates = rejected_type_predicates(expression=statement.test)

        membership_expectations = {
            _expectation_name(expression=node.comparators[0])
            for node in ast.walk(node=statement.test)
            if
            isinstance(node, ast.Compare) and
            (len(node.ops) == 1) and
            isinstance(node.ops[0], (ast.In, ast.NotIn)) and
            _type_call(expression=unwrap_binding(expression=node.left))
        }

        membership_expectations.update(
            _expectation_name(expression=node.args[1])
            for node in ast.walk(node=statement.test)
            if
            isinstance(node, ast.Call) and
            _builtin(expression=node.func, name="isinstance") and
            (len(node.args) == 2) and
            isinstance(unwrap_binding(expression=node.args[1]), ast.Tuple)
        )

        bound_expectations = [
            node.target.id
            for node in ast.walk(node=statement.test)
            if isinstance(node, ast.NamedExpr) and _expectation_name(expression=node) is not None
        ]

        reused_expectations = len(bound_expectations) != len(set(bound_expectations))

        for rejection in statement.body:
            if not isinstance(rejection, ast.Raise) or not isinstance(rejection.exc, (ast.Call, ast.Name)):
                continue

            if isinstance(rejection.exc, ast.Name) and rejection.exc.id not in {"TypeError", "ValueError", "RuntimeError"}:
                # Re-raising an existing failure preserves its original message
                # and identity; this rule governs newly constructed diagnostics.
                continue

            messages = []

            if isinstance(rejection.exc, ast.Call):
                messages.extend(value for value in [*rejection.exc.args, *(keyword.value for keyword in rejection.exc.keywords)] if isinstance(value, ast.JoinedStr))

            for predicate in predicates:
                subject = diagnostic_subject(subject=predicate.subject)
                expected_name = _expectation_name(expression=predicate.expected)
                unavailable = any(isinstance(node, ast.Name) and node.id in predicate.comprehension_names for node in ast.walk(node=subject))

                if has_type_rejection_alternatives(expression=statement.test, rejecting=True) or reused_expectations or expected_name is None or unavailable or not any(_has_type_pair(source=source, message=message, subject=subject, expected_name=expected_name, membership=expected_name in membership_expectations) for message in messages):
                    findings.append(f'{source_path}:{rejection.lineno}: bind the accepted type as expected_type and report f"{{type({ast.unparse(subject)})=}} ≠ {{expected_type=}}" from the same checked value and expectation')

    return findings


def _subclass_findings(*, source_path: pathlib.Path, source: str) -> list[str]:
    """Check both operands of direct subclass rejection without inferring prose."""
    findings = []

    for statement in ast.walk(node=ast.parse(source=source, filename=str(source_path))):
        if not isinstance(statement, ast.If) or len(statement.body) != 1:
            continue

        condition = statement.test

        if not isinstance(condition, ast.UnaryOp) or not isinstance(condition.op, ast.Not):
            continue

        predicate = condition.operand

        if not (
            isinstance(predicate, ast.Call) and
            _builtin(expression=predicate.func, name="issubclass") and
            (len(predicate.args) == 2) and
            (not predicate.keywords)
        ):
            continue

        rejection = statement.body[0]

        if not isinstance(rejection, ast.Raise) or rejection.exc is None:
            continue

        exception = rejection.exc.func if isinstance(rejection.exc, ast.Call) else rejection.exc

        if not _builtin(expression=exception, name="TypeError"):
            continue

        message = rejection.exc.args[0] if isinstance(rejection.exc, ast.Call) and rejection.exc.args else None

        evidenced = (
            {
                _subject_key(expression=field.value)
                for field in message.values
                if isinstance(field, ast.FormattedValue) and _is_debug_expression(source=source, formatted=field)
            }
            if isinstance(message, ast.JoinedStr) else set()
        )

        missing = [
            ast.unparse(ast_obj=diagnostic_subject(subject=operand))
            for operand in predicate.args
            if _subject_key(expression=operand) not in evidenced
        ]

        # The parent is evaluated after the candidate; rebinding a name used to
        # render the candidate would show a different value from the one checked
        candidate = diagnostic_subject(subject=predicate.args[0])

        candidate_names = {node.id for node in ast.walk(node=candidate) if isinstance(node, ast.Name)}

        if any(isinstance(node, ast.NamedExpr) and node.target.id in candidate_names for node in ast.walk(node=predicate.args[1])):
            findings.append(f"{source_path}:{rejection.lineno}: subclass parent binding overwrites the checked candidate; preserve its value for the diagnostic")
            continue

        if missing:
            findings.append(
                f"{source_path}:{rejection.lineno}: subclass rejection lacks debug f-string evidence for "
                f"{', '.join(missing)}; report the checked class and parent, following their bindings"
            )

    return sorted(findings)


class TypeErrorEvidencePostureTests(unittest.TestCase):
    def test_nonmembership_symbol_matches_type_membership_guards(self):
        for condition in (
            "not (type(seconds) in (expected_types := (int, float)))",
            "type(seconds) not in (expected_types := (int, float))",
            "not isinstance(seconds, (expected_types := (int, float)))",
        ):
            with self.subTest(condition=condition):
                source = (
                    f'if {condition}:\n'
                    f'    raise TypeError(f"{{type(seconds)=}} ∉ {{expected_types=}}")\n'
                )

                self.assertEqual(first=[], second=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

        source = (
            'if not (type(seconds) is (expected_type := float)):\n'
            '    raise TypeError(f"{type(seconds)=} ∉ {expected_type=}")\n'
        )

        self.assertTrue(expr=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_subclass_evidence_follows_both_bindings_without_requiring_names_or_prose(self):
        for (candidate, parent, message) in (
            ("candidate", "Parent", 'f"{candidate=} {Parent=}"'),
            ("candidate", "(expected_parent := Parent)", 'f"{candidate=} should be a subclass of {expected_parent=}"'),
            ("candidate", "(should_have_parent := Parent)", 'f"{candidate=} {should_have_parent=}"'),
            ("(actual := owner.candidate)", "(base := owner.parent)", 'f"{actual=} {base=}"'),
            ("owner.candidate", "owner.parent", 'f"{owner.candidate=} {owner.parent=}"'),
            ("candidate", "(bases := (Left, Right))", 'f"{bases=} {candidate=}"'),
        ):
            with self.subTest(candidate=candidate, parent=parent):
                source = (
                    f"if not builtins.issubclass({candidate}, {parent}):\n"
                    f"    raise builtins.TypeError({message})\n"
                )

                self.assertEqual(first=[], second=_subclass_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_subclass_evidence_rejects_missing_or_different_operands(self):
        for message in (
            '"candidate should inherit Parent"',
            'f"{candidate=}"',
            'f"{base=}"',
            'f"{other=} {base=}"',
            'f"{candidate=} {Parent=}"',
            'f"{type(candidate)=} {base=}"',
            'f"{candidate} {base=}"',
            'f"{candidate=} {base}"',
            'f"{{candidate=}} {base=}"',
            'f"{candidate:{base=}}"',
            'message',
        ):
            with self.subTest(message=message):
                source = (
                    f"if not issubclass(candidate, (base := Parent)):\n"
                    f"    raise TypeError({message})\n"
                )

                self.assertTrue(expr=_subclass_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_subclass_evidence_scope_is_direct_rejection_only(self):
        for source in (
            "if issubclass(candidate, Parent):\n"
            "    raise TypeError('unsupported')\n",
            "if not issubclass(candidate, Parent) or other:\n"
            "    raise TypeError('bad')\n",
            "if not issubclass(candidate, Parent):\n"
            "    raise ValueError('bad')\n",
            "if not issubclass(candidate, Parent):\n"
            "    raise failure\n",
            "if not issubclass(candidate, Parent):\n"
            "    raise\n",
            "if not custom.issubclass(candidate, Parent):\n"
            "    raise TypeError('bad')\n",
            "if not issubclass(candidate, Parent):\n"
            "    if enabled:\n"
            "        raise TypeError('bad')\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(first=[], second=_subclass_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_subclass_parent_binding_cannot_replace_the_candidate_evidence(self):
        for (candidate, parent, message) in (
            ("candidate", "(candidate := Parent)", 'f"{candidate=}"'),
            ("(actual := candidate)", "(actual := Parent)", 'f"{actual=}"'),
        ):
            with self.subTest(candidate=candidate, parent=parent):
                source = (
                    f"if not issubclass({candidate}, {parent}):\n"
                    f"    raise TypeError({message})\n"
                )

                self.assertTrue(expr=_subclass_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_subclass_evidence_diagnostic_identifies_both_missing_operands(self):
        source = (
            "if not issubclass(candidate, (base := Parent)):\n"
            "    raise TypeError\n"
        )

        findings = _subclass_findings(source_path=pathlib.Path("example.py"), source=source)

        self.assertEqual(first=1, second=len(findings))
        self.assertIn(member="example.py:2", container=findings[0])
        self.assertIn(member="candidate, base", container=findings[0])

    def test_bound_type_and_inequality_fields_are_required_together(self):
        source = (
            'if not (type(values) is (expected_type := tuple)):\n'
            '    raise TypeError(f"{type(values)=} ≠ {expected_type=}")\n'
        )

        self.assertEqual(first=[], second=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

        for bad_message in ['f"{type(values)=} != {expected_type=}"', 'f"{type(values)=} ≠ {tuple=}"', 'f"{type(values)} ≠ {expected_type=}"', 'f"{type(other)=} ≠ {expected_type=}"', 'f"{type(values)=} ≠ {expected_type}"']:
            with self.subTest(bad_message=bad_message):
                bad_source = source.replace('f"{type(values)=} ≠ {expected_type=}"', bad_message)

                self.assertTrue(expr=_expectation_findings(source_path=pathlib.Path("example.py"), source=bad_source))

    def test_compound_guards_cannot_overwrite_an_earlier_expectation(self):
        source = (
            'if type(left) is not (expected_type := int) and type(right) is not (expected_type := str):\n'
            '    raise Error(f"{type(left)=} ≠ {expected_type=}; {type(right)=} ≠ {expected_type=}")\n'
        )

        self.assertTrue(expr=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_value_only_failure_cannot_claim_a_type_mismatch(self):
        for condition in [
            "type(value) is not (expected_type := str) or forbidden",
            "not (type(value) is (expected_type := str) and permitted)",
        ]:
            source = (
                f'if {condition}:\n'
                f'    raise Error(f"{{type(value)=}} ≠ {{expected_type=}}")\n'
            )

            self.assertTrue(expr=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_joint_mismatches_can_truthfully_report_both_subjects(self):
        source = (
            'if type(left) is not (expected_left_type := int) and type(right) is not (expected_right_type := str):\n'
            '    raise Error(f"{type(left)=} ≠ {expected_left_type=}; {type(right)=} ≠ {expected_right_type=}")\n'
        )

        self.assertEqual(first=[], second=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_subclass_and_multiple_type_acceptance_are_preserved(self):
        cases = [
            ("not (isinstance(value, (expected_type := Base)))", "expected_type"),
            ("not (type(value) in (expected_types := (int, float)))", "expected_types"),
        ]

        for (condition, expected_name) in cases:
            source = (
                f'if {condition}:\n'
                f'    raise CustomError("invalid_type", f"{{type(value)=}} ≠ {{{expected_name}=}}")\n'
            )

            self.assertEqual(first=[], second=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_existing_expectation_authority_does_not_need_self_assignment(self):
        source = (
            'for expected_type in types:\n'
            '    if not (type(value) is expected_type):\n'
            '        raise TypeError(f"{type(value)=} ≠ {expected_type=}")\n'
        )

        self.assertEqual(first=[], second=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_rethrows_keep_the_original_exception(self):
        for rejection in ["raise", "raise failure"]:
            source = (
                f"if not (isinstance(failure, KnownError)):\n"
                f"    {rejection}\n"
            )

            self.assertEqual(first=[], second=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_generator_local_cannot_be_reported_as_an_outer_value(self):
        source = (
            'if not (all(type(value) is (expected_type := str) for value in values)):\n'
            '    raise TypeError(f"{type(value)=} ≠ {expected_type=}")\n'
        )

        self.assertTrue(expr=_expectation_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_genuine_debug_fields_show_the_rejected_type(self) -> None:
        for message in (
            'f"Unexpected {type(value)=}; expected str"',
            'f"Unexpected {type(value) = }; expected str"',
            'f"Unexpected {type(value)=!s}; expected str"',
            'f"Unexpected {type(value)=!a}; expected str"',
            'f"Unexpected {type(value)=!r:>20}; expected str"',
            'f"Unexpected {builtins.type(value)=}; expected str"',
            'f"Unexpected {(type(value))=}; expected str"',
            'f"Unexpected " f"{type(value)=}; expected str"',
            'f"Réception inattendue {type(value)=}; expected str"',
        ):
            for guard in ("type(value) is not str", "str != type(value)", "not isinstance(value, str)"):
                with self.subTest(message=message, guard=guard):
                    source = (
                        f"if {guard}:\n"
                        f"    raise TypeError({message})\n"
                    )

                    self.assertEqual(first=[], second=_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_static_expected_unrelated_and_manual_fields_do_not_prove_actual_type(self) -> None:
        for message in (
            '"value must be str"',
            'f"Unexpected value; expected {str=}"',
            'f"Unexpected {type(other)=}; expected str"',
            'f"Unexpected {type(str)=}; expected str"',
            'f"Unexpected {value=}; expected str"',
            'f"Unexpected {type(value)}; expected str"',
            'f"Unexpected type(value)={type(value)!r}; expected str"',
            'f"Unexpected type(value) = {type(value)!r}; expected str"',
            'f"Unexpected {{type(value)=}}; expected str"',
            'f"Unexpected {str(type(value))=}; expected str"',
            'f"Unexpected {type(value).__name__=}; expected str"',
            'f"Unexpected {custom.type(value)=}; expected str"',
            'f"Unexpected {value:{type(value)=}}; expected str"',
            'f"Unexpected {type(value)=}{type(other)=}" if enabled else "invalid"',
            'message',
        ):
            with self.subTest(message=message):
                source = (
                    f"if type(value) is not str:\n"
                    f"    raise TypeError({message})\n"
                )

                findings = _findings(source_path=pathlib.Path("example.py"), source=source)

                self.assertEqual(first=1, second=len(findings))
                self.assertEqual(first=("value",), second=findings[0].missing_subjects)

    def test_each_rejected_subject_requires_its_own_evidence(self) -> None:
        for guard in (
            "type(left) is not int or type(right) is not str",
            "not (isinstance(left, int) and isinstance(right, str))",
            "type(left) is not type(right)",
        ):
            with self.subTest(guard=guard):
                incomplete = (
                    f'if {guard}:\n'
                    f'    raise TypeError(f"Unexpected {{type(left)=}}")\n'
                )

                findings = _findings(source_path=pathlib.Path("example.py"), source=incomplete)

                self.assertEqual(first=("right",), second=findings[0].missing_subjects)

                complete = (
                    f'if {guard}:\n'
                    f'    raise TypeError(f"Unexpected {{type(left)=}} and {{type(right)=}}")\n'
                )

                self.assertEqual(first=[], second=_findings(source_path=pathlib.Path("example.py"), source=complete))

    def test_subject_expression_identity_is_preserved(self) -> None:
        source = (
            'if type(record.value) is not str:\n'
            '    raise TypeError(f"Unexpected {type(value)=}")\n'
        )

        self.assertEqual(
            first=("record.value",),
            second=_findings(source_path=pathlib.Path("example.py"), source=source)[0].missing_subjects,
        )

        source = (
            'if type(record["value"]) is not str:\n'
            '    raise TypeError(f"Unexpected {type(record[\'value\'])=}")\n'
        )

        self.assertEqual(first=[], second=_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_bare_builtin_type_errors_and_causes_are_checked(self) -> None:
        for rejection in ("raise TypeError", "raise builtins.TypeError()", "raise TypeError('bad') from cause"):
            with self.subTest(rejection=rejection):
                source = (
                    f"if type(value) is not str:\n"
                    f"    {rejection}\n"
                )

                self.assertEqual(first=1, second=len(_findings(source_path=pathlib.Path("example.py"), source=source)))

    def test_diagnostic_identifies_the_missing_subject_and_required_syntax(self) -> None:
        source = (
            "if type(value) is not str:\n"
            "    raise TypeError('bad')\n"
        )

        diagnostic = _findings(source_path=pathlib.Path("example.py"), source=source)[0].diagnostic()

        self.assertIn(member="example.py:2", container=diagnostic)
        self.assertIn(member="('value',)", container=diagnostic)
        self.assertIn(member="{type(value)=}", container=diagnostic)

    def test_unowned_nested_or_non_type_failures_are_outside_the_rule(self) -> None:
        for source in (
            "if value is None:\n"
            "    raise TypeError('missing')\n",
            "if isinstance(value, str):\n"
            "    raise TypeError('unsupported string')\n",
            "if type(value) is not str:\n"
            "    raise ValueError('bad')\n",
            "if type(value) is not str:\n"
            "    raise errors.TypeError('bad')\n",
            "if type(value) is not str:\n"
            "    if enabled:\n"
            "        raise TypeError('bad')\n",
            "if type(value) is not str:\n"
            "    def deferred():\n"
            "        raise TypeError('bad')\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(first=[], second=_findings(source_path=pathlib.Path("example.py"), source=source))

    def test_constrained_type_rejection_still_requires_actual_evidence(self):
        source = (
            "if type(value) is not str and optional:\n"
            "    raise TypeError('bad')\n"
        )

        self.assertEqual(first=1, second=len(_findings(source_path=pathlib.Path("example.py"), source=source)))

    def test_production_type_rejections_show_the_actual_offending_types(self) -> None:
        findings = [
            finding
            for source_path in python_sources(production_only=True)
            for finding in _findings(source_path=source_path.relative_to(REPOSITORY_ROOT), source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(finding.diagnostic() for finding in findings))

    def test_production_type_rejections_bind_and_report_the_same_expectation(self):
        findings = [
            finding
            for source_path in python_sources(production_only=True)
            for finding in _expectation_findings(source_path=source_path.relative_to(REPOSITORY_ROOT), source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))

    def test_production_subclass_rejections_report_both_checked_operands(self):
        findings = [
            finding
            for source_path in python_sources(production_only=True)
            for finding in _subclass_findings(source_path=source_path.relative_to(REPOSITORY_ROOT), source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(first=[], second=findings, msg="\n".join(findings))
