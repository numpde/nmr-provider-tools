"""Use shared type requirements instead of handwritten raising type guards.

Recognize builtin isinstance, issubclass, and exact-type comparisons in guards
whose body only constructs and raises an exception. Findings require review:
rejected-type branches need an accepted type, and compound guards must retain
their value checks, evaluation order, bindings, and failure semantics.
Aliases and shadowed builtins are outside this syntactic check's resolution.
"""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from .sources import ROOT, python_sources
from .test_type_error_posture import _builtin, _type_call
from .type_checks import unwrap_binding


def _required_helpers(expression: ast.expr) -> set[str]:
    """Identify direct type predicates without inspecting unrelated call arguments."""

    expression = unwrap_binding(expression=expression)

    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return _required_helpers(expression.operand)

    if isinstance(expression, ast.BoolOp):
        return {helper for operand in expression.values for helper in _required_helpers(operand)}

    if isinstance(expression, ast.Call) and (len(expression.args) == 2) and (not expression.keywords):
        for (predicate, helper) in (
            ("isinstance", "require_instance"),
            ("issubclass", "require_subclass"),
        ):
            if _builtin(expression=expression.func, name=predicate):
                return {helper}

    if (
        isinstance(expression, ast.Compare) and
        (len(expression.ops) == 1) and
        isinstance(expression.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq, ast.In, ast.NotIn)) and
        any(_type_call(expression=unwrap_binding(expression=operand)) for operand in (expression.left, *expression.comparators))
    ):
        return {"require_type"}

    return set()


def guard_findings(*, source: str) -> list[tuple[int, tuple[str, ...]]]:
    """Report single-raise type guards, excluding recovery and existing failures."""

    findings = []

    for statement in ast.walk(node=ast.parse(source=source)):
        if (not isinstance(statement, ast.If)) or (len(statement.body) != 1):
            continue

        rejection = statement.body[0]

        if (not isinstance(rejection, ast.Raise)) or (not isinstance(rejection.exc, ast.Call)):
            continue

        constructor = rejection.exc.func

        if isinstance(constructor, ast.Attribute) and (constructor.attr == "with_traceback"):
            continue

        helpers = _required_helpers(statement.test)

        if helpers:
            findings.append((statement.lineno, tuple(sorted(helpers))))

    return sorted(findings)


class TypeRequirementHelperPostureTests(unittest.TestCase):
    def test_builtin_type_guards_use_the_corresponding_helper(self):
        cases = (
            ("not isinstance(candidate, Parent)", "require_instance"),
            ("isinstance((admission := submission.submit()), NotAdmitted)", "require_instance"),
            ("not builtins.isinstance(candidate, (Parent, Other))", "require_instance"),
            ("not (type(candidate) is (expected_type := Parent))", "require_type"),
            ("type(candidate) is not Parent", "require_type"),
            ("Parent is not type(candidate)", "require_type"),
            ("type(candidate) != Parent", "require_type"),
            ("type(candidate) not in (Parent, Other)", "require_type"),
            ("not (builtins.type(candidate) in (expected_types := (int, float)))", "require_type"),
            ("not issubclass(candidate, Parent)", "require_subclass"),
            ("issubclass(candidate, Forbidden)", "require_subclass"),
            ("not builtins.issubclass(candidate, (Parent, Other))", "require_subclass"),
        )

        for (condition, helper) in cases:
            with self.subTest(condition=condition):
                findings = guard_findings(
                    source=f"if {condition}:\n"
                    f"    raise CustomError('invalid') from cause\n",
                )

                self.assertEqual([(1, (helper,))], findings)

    def test_multiline_and_compound_guards_are_review_findings(self):
        source = (
            "if not (\n"
            "    isinstance(candidate, Parent) and\n"
            "    issubclass(candidate_type, Parent) and\n"
            "    (type(count) is int) and\n"
            "    (count > 0)\n"
            "):\n"
            "    raise ValueError(\n"
            "        'invalid candidate or count'\n"
            "    )\n"
        )

        findings = guard_findings(source=source)

        self.assertEqual([1], [line for (line, _) in findings])

        self.assertEqual(("require_instance", "require_subclass", "require_type"), findings[0][1])

    def test_recovery_and_existing_failure_branches_are_outside_the_rule(self):
        sources = (
            (
                "if not isinstance(candidate, Parent):\n"
                "    return fallback\n"
            ),
            (
                "if not isinstance(candidate, Parent):\n"
                "    retry()\n"
            ),
            (
                "if not isinstance(candidate, Parent):\n"
                "    raise\n"
            ),
            (
                "if not isinstance(candidate, Parent):\n"
                "    raise failure\n"
            ),
            (
                "if not isinstance(candidate, Parent):\n"
                "    raise failure.with_traceback(traceback)\n"
            ),
            (
                "if not isinstance(candidate, Parent):\n"
                "    cleanup()\n"
                "    raise Error()\n"
            ),
            (
                "if not isinstance(candidate, Parent):\n"
                "    if invalid:\n"
                "        raise Error()\n"
            ),
            (
                "if not isinstance(candidate, Parent):\n"
                "    def later():\n"
                "        raise Error()\n"
            ),
            (
                "if invalid:\n"
                "    raise Error(f'{type(candidate)=}')\n"
            ),
        )

        for source in sources:
            with self.subTest(source=source):
                self.assertEqual([], guard_findings(source=source))

    def test_unrelated_calls_and_value_comparisons_are_outside_the_rule(self):
        conditions = (
            "custom.isinstance(candidate, Parent)",
            "custom.issubclass(candidate, Parent)",
            "custom.type(candidate) is Parent",
            "validate(type(candidate))",
            "any(isinstance(candidate, Parent) for candidate in candidates)",
            "type(candidate).__name__ == 'Parent'",
            "candidate is None",
        )

        for condition in conditions:
            with self.subTest(condition=condition):
                self.assertEqual(
                    [],
                    guard_findings(
                        source=f"if {condition}:\n"
                        f"    raise Error()\n",
                    ),
                )

    def test_authored_type_guards_use_shared_requirements(self):
        findings = []

        for source_path in python_sources(production_only=False):
            for (line, helpers) in guard_findings(source=source_path.read_text(encoding="utf-8")):
                findings.append(
                    f"{source_path.relative_to(ROOT)}:{line}: use {', '.join(helpers)} for the accepted type; "
                    f"preserve value checks, bindings, and failure semantics"
                )

        if findings:
            self.fail(f"{len(findings)} handwritten raising type guards:\n" + "\n".join(findings))
