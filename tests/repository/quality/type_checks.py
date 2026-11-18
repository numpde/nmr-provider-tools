"""Identify the actual values and type expectations owned by rejection predicates.

The scanner recognizes builtin type/isinstance syntax and its Boolean inverses;
it does not resolve aliases or infer types from variable names. Comprehension
bindings remain explicit so diagnostics cannot accidentally read a different
outer value with the same name.
"""

from tools.container_guard import require_container

require_container()

import ast
from dataclasses import dataclass, replace

from .test_type_error_posture import _builtin, _type_call


@dataclass(frozen=True, slots=True, kw_only=True)
class TypePredicate:
    subject: ast.expr
    expected: ast.expr
    comprehension_names: frozenset[str]


def unwrap_binding(*, expression: ast.expr) -> ast.expr:
    if isinstance(expression, ast.NamedExpr):
        return expression.value

    return expression


def diagnostic_subject(*, subject: ast.expr) -> ast.expr:
    if isinstance(subject, ast.NamedExpr):
        return subject.target

    return subject


def _predicates(*, expression: ast.expr, rejecting: bool, comprehension_names: frozenset[str]) -> list[TypePredicate]:
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return _predicates(expression=expression.operand, rejecting=not rejecting, comprehension_names=comprehension_names)

    if isinstance(expression, ast.BoolOp):
        return [
            predicate
            for value in expression.values
            for predicate in _predicates(expression=value, rejecting=rejecting, comprehension_names=comprehension_names)
        ]

    if isinstance(expression, ast.Call):
        if (not rejecting) and (_builtin(expression=expression.func, name="isinstance")) and (len(expression.args) == 2):
            return [TypePredicate(subject=expression.args[0], expected=expression.args[1], comprehension_names=comprehension_names)]

        if (
            (len(expression.args) == 1) and
            isinstance(expression.args[0], ast.GeneratorExp) and
            any(_builtin(expression=expression.func, name=name) for name in ("any", "all"))
        ):
            generator = expression.args[0]

            names = frozenset(
                node.id
                for clause in generator.generators
                for node in ast.walk(node=clause.target)
                if isinstance(node, ast.Name)
            )

            predicates = _predicates(expression=generator.elt, rejecting=rejecting, comprehension_names=comprehension_names | names)
            resolved = []

            for predicate in predicates:
                expected = predicate.expected

                if isinstance(expected, ast.Name):
                    for clause in generator.generators:
                        if isinstance(clause.target, ast.Name) and clause.target.id == expected.id and isinstance(clause.iter, ast.Name) and clause.iter.id.startswith("expected_"):
                            expected = clause.iter

                resolved.append(replace(predicate, expected=expected))

            return resolved

        return []

    if (not isinstance(expression, ast.Compare)) or (len(expression.ops) != 1):
        return []

    operator = expression.ops[0]

    if rejecting:
        accepted_operator = isinstance(operator, (ast.IsNot, ast.NotEq, ast.NotIn))
    else:
        accepted_operator = isinstance(operator, (ast.Is, ast.Eq, ast.In))

    if not accepted_operator:
        return []

    left = unwrap_binding(expression=expression.left)
    right = unwrap_binding(expression=expression.comparators[0])

    if _type_call(expression=left):
        return [TypePredicate(subject=left.args[0], expected=expression.comparators[0], comprehension_names=comprehension_names)]

    if (_type_call(expression=right)) and (not isinstance(operator, (ast.In, ast.NotIn))):
        return [TypePredicate(subject=right.args[0], expected=expression.left, comprehension_names=comprehension_names)]

    return []


def rejected_type_predicates(*, expression: ast.expr) -> list[TypePredicate]:
    """Return syntactically rejected type expectations in evaluation order."""

    return _predicates(expression=expression, rejecting=True, comprehension_names=frozenset())


def has_type_rejection_alternatives(*, expression: ast.expr, rejecting: bool) -> bool:
    """A shared raise cannot claim every type mismatch when one alternative suffices."""

    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return has_type_rejection_alternatives(expression=expression.operand, rejecting=not rejecting)

    if not isinstance(expression, ast.BoolOp):
        return False

    alternatives = (rejecting and isinstance(expression.op, ast.Or)) or ((not rejecting) and (isinstance(expression.op, ast.And)))

    if alternatives and _predicates(expression=expression, rejecting=rejecting, comprehension_names=frozenset()):
        return True

    return any(has_type_rejection_alternatives(expression=operand, rejecting=rejecting) for operand in expression.values)
