# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/test_validation_guard_posture.py
# Original detector admission, fixtures and assertions retained; local rendering makes AST inversion explicit.
"""Flag narrow guard shapes whose accepted condition merits review."""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path

from .sources import REPOSITORY_ROOT, python_sources

# This checker audits its own implementation using its loaded source path.
# repo-check: allow-module-constant
_CHECK_PATH = Path(__file__).resolve()


@dataclass(frozen=True, slots=True)
class _Finding:
    path: Path
    line: int
    rejected_condition: str
    concern: str
    candidate_guard: str
    caveat: str | None = None

    def diagnostic(self) -> str:
        diagnostic = (
            f"{self.path}:{self.line}: guard {self.rejected_condition!r} "
            f"{self.concern}; review whether {self.candidate_guard!r} makes "
            "the accepted condition explicit"
        )
        if self.caveat is not None:
            diagnostic += f" ({self.caveat})"
        return diagnostic


def _direct_value_error(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Raise):
        return False
    exception = statement.exc
    return (isinstance(exception, ast.Name) and exception.id == "ValueError") or (
        isinstance(exception, ast.Call) and isinstance(exception.func, ast.Name) and exception.func.id == "ValueError"
    )


# These AST types define the finite ordered-rejection grammar shared by detectors.
# repo-check: allow-module-constant
_ORDERED_REJECTION_OPERATORS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _accepted_ordering(comparison: ast.Compare) -> str:
    left = ast.unparse(comparison.left)
    right = ast.unparse(comparison.comparators[0])
    operator = comparison.ops[0]
    if isinstance(operator, ast.Lt):
        return f"{right} <= {left}"
    if isinstance(operator, ast.LtE):
        return f"{right} < {left}"
    if isinstance(operator, ast.Gt):
        return f"{left} <= {right}"
    if isinstance(operator, ast.GtE):
        return f"{left} < {right}"
    raise AssertionError("comparison must use a reviewed ordered operator")


def _rejected_ordering_finding(
    *,
    path: Path,
    statement: ast.If,
) -> _Finding | None:
    # Validation states the accepted domain once, in bound-to-value order, and
    # rejects its exact inverse. Do not replace that contract with a familiar
    # enumeration of rejected values merely because it is often equivalent for
    # the types currently admitted at the boundary.
    if not (
        not statement.orelse
        and len(statement.body) == 1
        and _direct_value_error(statement.body[0])
        and isinstance(statement.test, ast.Compare)
        and len(statement.test.ops) == 1
        and isinstance(statement.test.ops[0], _ORDERED_REJECTION_OPERATORS)
        and len(statement.test.comparators) == 1
    ):
        return None
    return _Finding(
        path=path,
        line=statement.lineno,
        rejected_condition=ast.unparse(statement.test),
        concern="uses a rejected-condition comparison",
        candidate_guard=f"if not ({_accepted_ordering(statement.test)})",
        caveat=("ordered comparisons are not assumed equivalent for every Python value"),
    )


def _split_range_finding(
    *,
    path: Path,
    statement: ast.If,
) -> _Finding | None:
    if not (
        not statement.orelse
        and len(statement.body) == 1
        and _direct_value_error(statement.body[0])
        and isinstance(statement.test, ast.BoolOp)
        and isinstance(statement.test.op, ast.Or)
        and len(statement.test.values) == 2
        and all(
            isinstance(comparison, ast.Compare)
            and len(comparison.ops) == 1
            and isinstance(comparison.ops[0], _ORDERED_REJECTION_OPERATORS)
            and len(comparison.comparators) == 1
            for comparison in statement.test.values
        )
    ):
        return None
    lower_rejection, upper_rejection = statement.test.values
    if not (
        isinstance(lower_rejection.ops[0], (ast.Lt, ast.LtE))
        and isinstance(upper_rejection.ops[0], (ast.Gt, ast.GtE))
        and ast.dump(lower_rejection.left) == ast.dump(upper_rejection.left)
    ):
        return None
    subject = ast.unparse(lower_rejection.left)
    lower = ast.unparse(lower_rejection.comparators[0])
    upper = ast.unparse(upper_rejection.comparators[0])
    inverse_operators = {
        ast.Lt: "<=",
        ast.LtE: "<",
        ast.Gt: "<=",
        ast.GtE: "<",
    }
    # The bounds are admitted above. Select their inverse symbols without a
    # fallback, retaining isinstance precedence for subclasses of AST operators.
    for operator_type in (ast.Lt, ast.LtE):
        if isinstance(lower_rejection.ops[0], operator_type):
            lower_operator = inverse_operators[operator_type]
            break
    for operator_type in (ast.Gt, ast.GtE):
        if isinstance(upper_rejection.ops[0], operator_type):
            upper_operator = inverse_operators[operator_type]
            break
    accepted_range = f"{lower} {lower_operator} {subject} {upper_operator} {upper}"
    return _Finding(
        path=path,
        line=statement.lineno,
        rejected_condition=ast.unparse(statement.test),
        concern="splits one accepted interval into rejected bounds",
        candidate_guard=f"if not ({accepted_range})",
    )


def _accepted_atom(node: ast.expr) -> str | None:
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        if isinstance(node.ops[0], _ORDERED_REJECTION_OPERATORS):
            return _accepted_ordering(node)
        left = ast.unparse(node.left)
        right = ast.unparse(node.comparators[0])
        inverse_operators = {
            ast.Eq: "!=",
            ast.NotEq: "==",
            ast.Is: "is not",
            ast.IsNot: "is",
            ast.In: "not in",
            ast.NotIn: "in",
        }
        for operator_type, rendered in inverse_operators.items():
            if isinstance(node.ops[0], operator_type):
                return f"{left} {rendered} {right}"
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return ast.unparse(node.operand)
    return None


def _mixed_ordering_finding(
    *,
    path: Path,
    statement: ast.If,
) -> _Finding | None:
    if not (
        not statement.orelse
        and len(statement.body) == 1
        and _direct_value_error(statement.body[0])
        and isinstance(statement.test, ast.BoolOp)
        and isinstance(statement.test.op, (ast.Or, ast.And))
        and any(
            isinstance(value, ast.Compare) and len(value.ops) == 1 and isinstance(value.ops[0], _ORDERED_REJECTION_OPERATORS)
            for value in statement.test.values
        )
        and _split_range_finding(path=path, statement=statement) is None
    ):
        return None
    accepted_atoms = [_accepted_atom(value) for value in statement.test.values]
    if any(atom is None for atom in accepted_atoms):
        return None
    inverse_boolean_operators = {ast.Or: " and ", ast.And: " or "}
    for operator_type, rendered in inverse_boolean_operators.items():
        if isinstance(statement.test.op, operator_type):
            conjunction = rendered
            break
    accepted = conjunction.join(
        f"({atom})" if " and " in atom or " or " in atom else atom for atom in accepted_atoms if atom is not None
    )
    return _Finding(
        path=path,
        line=statement.lineno,
        rejected_condition=ast.unparse(statement.test),
        concern="combines rejected terms instead of stating the accepted domain",
        candidate_guard=f"if not ({accepted})",
    )


def _none_rejected_subject(node: ast.expr) -> ast.expr | None:
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Is)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value is None
    ):
        return node.left
    return None


def _negated_predicate_subject(node: ast.expr) -> ast.expr | None:
    if not (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)):
        return None
    predicate = node.operand
    if isinstance(predicate, ast.Attribute):
        return predicate.value
    if isinstance(predicate, ast.Call) and isinstance(predicate.func, ast.Attribute):
        return predicate.func.value
    return None


def _returns_none(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Return) and (
        statement.value is None or (isinstance(statement.value, ast.Constant) and statement.value.value is None)
    )


def _inverse_optional_predicate_finding(
    *,
    path: Path,
    statement: ast.If,
) -> _Finding | None:
    if not (
        not statement.orelse
        and len(statement.body) == 1
        and _returns_none(statement.body[0])
        and isinstance(statement.test, ast.BoolOp)
        and isinstance(statement.test.op, ast.Or)
        and len(statement.test.values) == 2
    ):
        return None
    rejected_subject = _none_rejected_subject(statement.test.values[0])
    predicate_subject = _negated_predicate_subject(statement.test.values[1])
    if not (
        rejected_subject is not None
        and predicate_subject is not None
        and ast.dump(rejected_subject) == ast.dump(predicate_subject)
    ):
        return None
    subject = ast.unparse(rejected_subject)
    predicate = ast.unparse(statement.test.values[1].operand)
    return _Finding(
        path=path,
        line=statement.lineno,
        rejected_condition=ast.unparse(statement.test),
        concern="disjoins absence from a negated predicate",
        candidate_guard=f"if not (({subject} is not None) and {predicate})",
    )


def _findings(path: Path, source: str) -> list[_Finding]:
    tree = ast.parse(source, filename=str(path))
    findings: list[_Finding] = []
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.If):
            continue
        for detector in (
            _rejected_ordering_finding,
            _split_range_finding,
            _mixed_ordering_finding,
            _inverse_optional_predicate_finding,
        ):
            finding = detector(path=path, statement=statement)
            if finding is not None:
                findings.append(finding)
    return findings


def _scan(paths: list[Path]) -> list[_Finding]:
    return sorted(
        [
            finding
            for path in paths
            for finding in _findings(
                path.relative_to(REPOSITORY_ROOT),
                path.read_text(encoding="utf-8"),
            )
        ],
        key=lambda finding: (finding.path, finding.line),
    )


class GuardPostureTests(unittest.TestCase):
    def test_mixed_ordering_renders_both_boolean_inverses(self) -> None:
        cases = (
            ("or", "if not (math.isfinite(value) and 0 < value)"),
            ("and", "if not (math.isfinite(value) or 0 < value)"),
        )
        for operator, expected in cases:
            with self.subTest(operator=operator):
                source = f"if not math.isfinite(value) {operator} value <= 0:\n    raise ValueError('invalid value')\n"
                findings = _findings(Path("example.py"), source)
                self.assertEqual(1, len(findings))
                self.assertEqual(expected, findings[0].candidate_guard)

    def test_split_range_renders_each_admitted_bound_strictness(self) -> None:
        cases = (
            ("<", ">", "if not (minimum <= value <= maximum)"),
            ("<", ">=", "if not (minimum <= value < maximum)"),
            ("<=", ">", "if not (minimum < value <= maximum)"),
            ("<=", ">=", "if not (minimum < value < maximum)"),
        )
        for lower, upper, expected in cases:
            with self.subTest(lower=lower, upper=upper):
                source = f"if value {lower} minimum or value {upper} maximum:\n    raise ValueError('outside bounds')\n"
                findings = _findings(Path("example.py"), source)
                self.assertEqual(1, len(findings))
                self.assertEqual(expected, findings[0].candidate_guard)

    def test_split_range_rejects_unsupported_comparison_operators(self) -> None:
        for condition in (
            "value == minimum or value > maximum",
            "value < minimum or value != maximum",
        ):
            with self.subTest(condition=condition):
                statement = ast.parse(f"if {condition}:\n    raise ValueError('outside bounds')\n").body[0]
                self.assertIsNone(_split_range_finding(path=Path("example.py"), statement=statement))

    def test_detector_flags_direct_rejected_orderings_and_split_ranges(self) -> None:
        source = textwrap.dedent(
            """
            def rejected(value, window, created_at, expires_at, minimum, maximum):
                if value <= 0:
                    raise ValueError("value must be positive")
                if window <= timedelta(0):
                    raise ValueError("window must be positive")
                if value > maximum:
                    raise ValueError("value exceeds maximum")
                if expires_at <= created_at:
                    raise ValueError("expiry must follow creation")
                if value < minimum or value > maximum:
                    raise ValueError("value is outside bounds")

            def other_ordered_rejections(value, depth, maximum_depth):
                if not (value > 0):
                    raise ValueError("value must be positive")
                if depth > maximum_depth:
                    raise ValueError("maximum depth exceeded")
                if value <= 0:
                    record_invalid_value(value)
                    raise ValueError("value must be positive")
                if value <= lower_bound:
                    raise ValueError("value is too small")
                if value is None or value <= 0:
                    raise ValueError("value must be positive when present")
                if value <= 0:
                    raise RuntimeError("runtime state is invalid")
            """
        )

        findings = _findings(Path("example.py"), source)

        self.assertEqual(
            [finding.candidate_guard for finding in findings],
            [
                "if not (0 < value)",
                "if not (timedelta(0) < window)",
                "if not (value <= maximum)",
                "if not (created_at < expires_at)",
                "if not (minimum <= value <= maximum)",
                "if not (depth <= maximum_depth)",
                "if not (lower_bound < value)",
                "if not (value is not None and 0 < value)",
            ],
        )

    def test_detector_flags_the_exact_inverse_optional_predicate_guard(self) -> None:
        source = textwrap.dedent(
            """
            def rejected(message):
                if message is None or not message.endswith("expected"):
                    return None

            def outside_the_narrow_rule(message, configured, cancelled):
                if not ((message is not None) and message.endswith("expected")):
                    return None
                if message is None or not configured:
                    return None
                if message is None or not message.endswith("expected") or cancelled:
                    return None
                if message is None or not message.endswith("expected"):
                    return "fallback"
                if not message.endswith("expected") or message is None:
                    return None
            """
        )

        findings = _findings(Path("example.py"), source)

        self.assertEqual(1, len(findings))
        self.assertEqual(
            "if not ((message is not None) and message.endswith('expected'))",
            findings[0].candidate_guard,
        )

    def test_detector_inverts_mixed_ordered_rejection_terms(self) -> None:
        source = textwrap.dedent(
            """
            def rejected(value, expires_at, valid_from):
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("value must be finite and positive")
                if type(value) is not int or value < 1:
                    raise ValueError("value must be a positive integer")
                if expires_at is not None and expires_at <= valid_from:
                    raise ValueError("expiry must follow validity")
            """
        )

        findings = _findings(Path("example.py"), source)

        self.assertEqual(
            [finding.candidate_guard for finding in findings],
            [
                "if not (math.isfinite(value) and 0 < value)",
                "if not (type(value) is int and 1 <= value)",
                "if not (expires_at is None or valid_from < expires_at)",
            ],
        )

    def test_check_implementation_has_no_findings(self) -> None:
        self.assertEqual([], _scan([_CHECK_PATH]))

    def test_nmr_api_has_no_guard_posture_findings(self) -> None:
        findings = _scan(python_sources(production_only=True))
        if findings:
            self.fail("guards needing accepted-condition review:\n" + "\n".join(finding.diagnostic() for finding in findings))
