"""Keep wrong-type rejection separate from value validation.

This syntactic check recognizes direct builtin type predicates in rejection
guards. It does not resolve aliases, shadowed builtins, or helper predicates.
"""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import textwrap
import unittest
import pathlib
from dataclasses import dataclass

from .sources import REPOSITORY_ROOT, python_sources


@dataclass(frozen=True, slots=True)
class _Finding:
    source_path: pathlib.Path
    line: int
    condition: str
    exception: str

    def diagnostic(self) -> str:
        concerns = {
            "ValueError": "rejects a type with ValueError",
            "TypeError": "combines type and value rejection with TypeError",
        }

        return (
            f"{self.source_path}:{self.line}: guard {self.condition!r} {concerns[self.exception]}; "
            f"reject wrong types first in a separate TypeError "
            f"guard, then validate supported values with ValueError"
        )


def _builtin(*, expression: ast.expr, name: str) -> bool:
    return (isinstance(expression, ast.Name) and expression.id == name) or (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Name)
        and expression.value.id == "builtins"
        and expression.attr == name
    )


def _type_call(*, expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Call)
        and _builtin(expression=expression.func, name="type")
        and len(expression.args) == 1
        and not expression.keywords
    )


def _type_rejection_operands(*, expression: ast.expr, rejecting: bool) -> list[ast.expr]:
    """Return subjects rejected through OR, or its accepted AND inverse.

    An unrelated AND condition can restrict a negative type predicate to a
    narrower domain. Such guards need semantic review rather than this rule.
    """

    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return _type_rejection_operands(expression=expression.operand, rejecting=not rejecting)

    if isinstance(expression, ast.BoolOp):
        if (rejecting and isinstance(expression.op, ast.Or)) or (
            not rejecting and isinstance(expression.op, ast.And)
        ):
            operands = []

            for value in expression.values:
                operands.extend(_type_rejection_operands(expression=value, rejecting=rejecting))

            return operands

        return []

    if isinstance(expression, ast.Call):
        if (
            not rejecting
            and _builtin(expression=expression.func, name="isinstance")
            and len(expression.args) == 2
            and not expression.keywords
        ):
            return [expression.args[0]]

        return []

    if not (
        isinstance(expression, ast.Compare)
        and len(expression.ops) == 1
        and len(expression.comparators) == 1
        and (_type_call(expression=expression.left) or _type_call(expression=expression.comparators[0]))
    ):
        return []

    operator = expression.ops[0]

    if (rejecting and isinstance(operator, ast.NotIn)) or (not rejecting and isinstance(operator, ast.In)):
        if _type_call(expression=expression.left):
            return [expression.left.args[0]]

        return []

    if (rejecting and isinstance(operator, (ast.IsNot, ast.NotEq))) or (
        not rejecting and isinstance(operator, (ast.Is, ast.Eq))
    ):
        return [
            operand.args[0]
            for operand in (expression.left, expression.comparators[0])
            if _type_call(expression=operand)
        ]

    return []


def _type_rejection(*, expression: ast.expr, rejecting: bool) -> bool:
    return bool(_type_rejection_operands(expression=expression, rejecting=rejecting))


def _value_predicate(*, expression: ast.expr) -> bool:
    """Recognize value tests without classifying unknown predicates by exclusion."""

    if _type_rejection(expression=expression, rejecting=True) or _type_rejection(expression=expression, rejecting=False):
        if not isinstance(expression, (ast.BoolOp, ast.UnaryOp)):
            return False

    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return _value_predicate(expression=expression.operand)

    if isinstance(expression, ast.BoolOp):
        return any(_value_predicate(expression=value) for value in expression.values)

    if isinstance(expression, (ast.Name, ast.Attribute, ast.Subscript)):
        return True

    if isinstance(expression, ast.Compare):
        if any(isinstance(operator, (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for operator in expression.ops):
            return True

        # Identity with None commonly tests regex match results, but identity
        # with a class object can instead restrict the supported type domain.
        return any(_value_predicate(expression=value) for value in (expression.left, *expression.comparators) if isinstance(value, ast.Call))

    if not isinstance(expression, ast.Call):
        return False

    if (
        any(_builtin(expression=expression.func, name=name) for name in ("any", "all"))
        and len(expression.args) == 1
        and not expression.keywords
        and isinstance(expression.args[0], ast.GeneratorExp)
    ):
        generator = expression.args[0]

        return _value_predicate(expression=generator.elt)

    return _builtin(expression=expression.func, name="len") or (
        isinstance(expression.func, ast.Attribute)
        and isinstance(expression.func.value, ast.Name)
        and expression.func.value.id == "re"
        and expression.func.attr in {"fullmatch", "match", "search"}
    )


def _findings(*, source_path: pathlib.Path, source: str) -> list[_Finding]:
    findings = []

    for statement in ast.walk(ast.parse(source=source, filename=str(source_path))):
        if not isinstance(statement, ast.If) or not _type_rejection(expression=statement.test, rejecting=True):
            continue

        # Only direct raises belong to this guard; nested scopes or conditional
        # branches can have their own failure contract.
        for child in statement.body:
            if not isinstance(child, ast.Raise) or child.exc is None:
                continue

            exception = child.exc.func if isinstance(child.exc, ast.Call) else child.exc

            if _builtin(expression=exception, name="ValueError"):
                exception_name = "ValueError"
            elif _builtin(expression=exception, name="TypeError") and _value_predicate(expression=statement.test):
                exception_name = "TypeError"
            else:
                continue

            findings.append(
                _Finding(source_path=source_path, line=statement.lineno, condition=ast.unparse(statement.test), exception=exception_name)
            )

            break

    return sorted(findings, key=lambda finding: finding.line)


class TypeErrorPostureTests(unittest.TestCase):
    def test_wrong_type_rejection_uses_type_error_before_semantic_validation(self) -> None:
        conditions = (
            'type(value) is not str or re.fullmatch(r"(?:sha256:|configured-v[23]:sha256:)[0-9a-f]{64}\\Z", value) is None',
            "type(value) is not str",
            "type(value) != str",
            "str is not type(value)",
            "str != type(value)",
            "not isinstance(value, str)",
            "type(value) not in (int, float)",
            "not (type(value) in (expected_types := (int, float)))",
            "not isinstance(value, (str, bytes)) or not value",
            "value == '' or not isinstance(value, str)",
            "not (type(value) is str and valid(value))",
            "not (isinstance(value, str) and valid(value))",
            "type(value) is not tuple or any(type(item) is not str or not item for item in value)",
            "type(value) is not tuple or not all(isinstance(item, str) and item for item in value)",
            "not (valid(value) and isinstance(value, str))",
            "not (str == type(value) and valid(value))",
            "not (isinstance(value, str) and (valid(value) and enabled))",
            "builtins.type(value) is not str or not value",
            "not builtins.isinstance(value, str) or not value",
            "type(record.value) is not expected_type or invalid(record.value)",
            "type(value) is not type(other)",
        )

        for condition in conditions:
            with self.subTest(condition=condition):
                source = f"if {condition}:\n    raise ValueError('invalid value')\n"
                findings = _findings(source_path=pathlib.Path("example.py"), source=source)

                self.assertEqual(1, len(findings))
                self.assertEqual(1, findings[0].line)

    def test_split_type_and_value_guards_are_accepted(self) -> None:
        source = textwrap.dedent(
            """
            def validate(value):
                if type(value) is not str:
                    raise TypeError("value must be a string")
                if re.fullmatch(r"[0-9a-f]{64}\\Z", value) is None:
                    raise ValueError("value must be a SHA-256 digest")
            """
        )

        self.assertEqual([], _findings(source_path=pathlib.Path("example.py"), source=source))

    def test_mixed_guard_cannot_be_fixed_by_changing_only_the_exception(self) -> None:
        for condition in (
            "type(value) is not str or not value",
            "not isinstance(value, str) or len(value) > 12",
            "not (type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None)",
            "not (isinstance(value, str) and 0 < len(value))",
        ):
            with self.subTest(condition=condition):
                source = f"if {condition}:\n    raise TypeError('invalid value')\n"
                findings = _findings(source_path=pathlib.Path("example.py"), source=source)

                self.assertEqual(1, len(findings))
                self.assertEqual("TypeError", findings[0].exception)
                self.assertIn("combines type and value rejection with TypeError", findings[0].diagnostic())

    def test_type_error_can_reject_only_types_in_a_boolean_guard(self) -> None:
        for condition in (
            "not isinstance(value, (str, bytes))",
            "type(value) is not int or type(other) is not str",
            "not (isinstance(value, int) and isinstance(other, str))",
            "not isinstance(value, int) or isinstance(value, bool)",
            "type(value) is not str or (type(other) is not int and type(other) is not float)",
            "type(value) is not tuple or any(type(item) is not str for item in value)",
            "type(value) is not tuple or not all(isinstance(item, str) for item in value)",
            "not isinstance(constructed_type, type) or constructed_type is NoneType",
            "not (isinstance(value_type, type) and is_dataclass(value_type))",
            "not isinstance(event, Event) or not callable(callback)",
            "type(value) is not str or not custom_predicate(value)",
        ):
            with self.subTest(condition=condition):
                source = f"if {condition}:\n    raise TypeError('invalid type')\n"

                self.assertEqual([], _findings(source_path=pathlib.Path("example.py"), source=source))

    def test_semantic_predicates_and_type_metadata_are_not_type_guards(self) -> None:
        conditions = (
            "not value or not valid(value)",
            "value is None or not valid(value)",
            "value == 'type(value) is not str' or not valid(value)",
            "record.type is not str or not valid(value)",
            "type_name != 'str' or not valid(value)",
            "not math.isfinite(value) or value <= 0",
            "type(value) is str and not valid(value)",
            "isinstance(value, str) and not valid(value)",
            "not (type(value) is str or valid(value))",
            "type(value) is not str and optional",
            "kind(value) is not str or not valid(value)",
            "custom.type(value) is not str or not valid(value)",
            "not custom.isinstance(value, str) or not valid(value)",
            "type(value) in allowed_types",
            "type(value) is expected_type is other_type",
            "type('Dynamic', (), {}) is not str",
        )

        for condition in conditions:
            with self.subTest(condition=condition):
                source = f"if {condition}:\n    raise ValueError('invalid value')\n"

                self.assertEqual([], _findings(source_path=pathlib.Path("example.py"), source=source))

    def test_only_the_guard_owning_a_direct_value_error_is_flagged(self) -> None:
        for rejection in (
            "raise ValueError",
            "raise ValueError('invalid') from cause",
            "raise builtins.ValueError('invalid')",
            "report(value)\n    raise ValueError('invalid')",
        ):
            with self.subTest(rejection=rejection):
                source = f"if type(value) is not str:\n    {rejection}\n"

                self.assertEqual(1, len(_findings(source_path=pathlib.Path("example.py"), source=source)))

        for rejection in (
            "raise TypeError('invalid')",
            "raise errors.ValueError('invalid')",
            "return ValueError('invalid')",
            "if invalid(value):\n        raise ValueError('invalid')",
            "def deferred():\n        raise ValueError('invalid')",
            "class Deferred:\n        raise ValueError('invalid')",
        ):
            with self.subTest(rejection=rejection):
                source = f"if type(value) is not str:\n    {rejection}\n"

                self.assertEqual([], _findings(source_path=pathlib.Path("example.py"), source=source))

    def test_diagnostic_names_the_guard_and_required_exception_order(self) -> None:
        findings = _findings(source_path=pathlib.Path("example.py"), source="if type(value) is not str:\n    raise ValueError('bad')\n")

        self.assertEqual(
            "example.py:1: guard 'type(value) is not str' rejects a type with ValueError; "
            "reject wrong types first in a separate TypeError guard, then validate supported values with ValueError",
            findings[0].diagnostic(),
        )

    def test_production_type_rejections_do_not_raise_value_error(self) -> None:
        findings = [
            finding
            for source_path in python_sources(production_only=True)
            for finding in _findings(source_path=source_path.relative_to(REPOSITORY_ROOT), source=source_path.read_text(encoding="utf-8"))
        ]

        self.assertEqual([], findings, msg="\n".join(finding.diagnostic() for finding in findings))
