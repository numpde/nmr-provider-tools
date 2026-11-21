"""Use the shared finite-number requirement in scalar rejection guards.

Inspect direct math.isfinite clauses under not and Boolean operators, preserving
their polarity. Finite filters, vector predicates and unrelated call arguments
are outside this rule. Aliases and shadowed module names are not resolved.
Calculation postconditions retain their builtin arithmetic exceptions. This
classification follows the raised exception type, not operand provenance.
Findings need review of exception classification, bindings and evaluation order;
require_finite_number already includes the numeric type requirement.
"""

from tools.container_guard import require_container

require_container()

import ast
from collections.abc import Iterator
import pathlib
import unittest

from operations.checker.findings import assert_no_findings

from .sources import CHECKER_ROOT, ROOT, python_sources
from .test_type_error_posture import _builtin
from .type_checks import raising_guards, unwrap_binding


def _finite_predicates(*, expression: ast.expr, rejecting: bool) -> Iterator[ast.Call]:
    """Yield scalar predicates whose false result contributes to rejection."""

    expression = unwrap_binding(expression=expression)

    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        yield from _finite_predicates(expression=expression.operand, rejecting=not rejecting)
    elif isinstance(expression, ast.BoolOp):
        for operand in expression.values:
            yield from _finite_predicates(expression=operand, rejecting=rejecting)
    elif (
        (not rejecting) and
        isinstance(expression, ast.Call) and
        isinstance(expression.func, ast.Attribute) and
        isinstance(expression.func.value, ast.Name) and
        (expression.func.value.id == "math") and
        (expression.func.attr == "isfinite") and
        (len(expression.args) == 1) and
        (not isinstance(expression.args[0], ast.Starred)) and
        (not expression.keywords)
    ):
        yield expression


# silent-defaults: allow-contract -- Anonymous specimens have no shared-helper ownership exemption
def guard_findings(*, source: str, source_path: pathlib.Path | None = None) -> list[tuple[int, str]]:
    """Locate each handwritten finite-number requirement outside its owner."""

    parsed_source = ast.parse(source=source)
    owned_nodes = set()

    if source_path == (ROOT / "packages/provider-core/src/provider_core/tools.py"):
        for declaration in parsed_source.body:
            if isinstance(declaration, ast.FunctionDef) and (declaration.name == "require_finite_number"):
                owned_nodes.update(ast.walk(node=declaration))

    return sorted(
        (
            (predicate.lineno, ast.unparse(predicate.args[0]))
            for statement in raising_guards(parsed_source=parsed_source)
            if statement not in owned_nodes
            if not any(
                (
                    _builtin(expression=statement.body[0].exc.func, name=exception_name)
                    for exception_name in ["ArithmeticError", "FloatingPointError", "OverflowError", "ZeroDivisionError"]
                ),
            )
            for predicate in _finite_predicates(expression=statement.test, rejecting=True)
        ),
        key=lambda finding: finding[0],
    )


class FiniteNumberHelperPostureTests(unittest.TestCase):
    def test_finiteness_clause_is_reported_inside_a_numeric_rejection(self):
        source = (
            "if (type(value) not in (int, float)) or (not math.isfinite(value)):\n"
            "    raise ValueError(f'{label} must be a finite number')\n"
        )

        self.assertEqual([(1, "value")], guard_findings(source=source))

    def test_negated_acceptance_reports_each_finiteness_clause(self):
        source = (
            "if not (\n"
            "    math.isfinite(initial_seconds) and\n"
            "    math.isfinite(maximum_seconds) and\n"
            "    initial_seconds <= maximum_seconds\n"
            "):\n"
            "    raise ValueError('invalid delays')\n"
        )

        self.assertEqual([(2, "initial_seconds"), (3, "maximum_seconds")], guard_findings(source=source))

        same_line = (
            "if not (math.isfinite(maximum_seconds) and math.isfinite(initial_seconds)):\n"
            "    raise ValueError('invalid delays')\n"
        )

        self.assertEqual([(1, "maximum_seconds"), (1, "initial_seconds")], guard_findings(source=same_line))

    def test_only_the_actual_shared_implementation_owns_its_predicate(self):
        source = (
            "def require_finite_number(number):\n"
            "    if not math.isfinite(number):\n"
            "        raise ValueError('nonfinite')\n"
            "def copied(seconds):\n"
            "    if not math.isfinite(seconds):\n"
            "        raise ValueError('nonfinite')\n"
        )

        self.assertEqual(
            [(5, "seconds")],
            guard_findings(source=source, source_path=ROOT / "packages/provider-core/src/provider_core/tools.py"),
        )

        self.assertEqual(
            [(2, "number"), (5, "seconds")],
            guard_findings(source=source, source_path=ROOT / "packages/consumer/tools.py"),
        )

    def test_filters_vector_predicates_and_existing_failures_are_not_requirements(self):
        fixture_path = CHECKER_ROOT / "tests/repository/fixtures/quality/finite_number/nonrequirements.py.fixture"

        self.assertEqual([], guard_findings(source=fixture_path.read_text(encoding="utf-8")))

    def test_arithmetic_postconditions_are_distinct_from_input_validation(self):
        fixture_path = CHECKER_ROOT / "tests/repository/fixtures/quality/finite_number/arithmetic_boundaries.py.fixture"

        self.assertEqual(
            [(12, "received_number"), (17, "computed_total")],
            guard_findings(source=fixture_path.read_text(encoding="utf-8")),
        )

    def test_authored_finite_number_guards_use_the_shared_requirement(self):
        findings = []

        for source_path in python_sources(production_only=False):
            for (line, subject) in guard_findings(source=source_path.read_text(encoding="utf-8"), source_path=source_path):
                findings.append(
                    f"{source_path.relative_to(ROOT)}:{line}: use require_finite_number({subject}) for this finite-number requirement; "
                    f"preserve exception classification, bindings and evaluation order"
                )

        assert_no_findings(findings)
