"""Flag literal-only messages in direct raising guards.

Inspect message-first exception calls and explicit message/reason keywords.
Causes, structured context, guard polarity and constant conditions do not exempt
a literal message. This rule does not judge nonliteral message quality.
A standalone allow-literal-guard-error comment immediately above the guard,
at the same indentation, must explain why its fixed message should remain.
"""

from tools.container_guard import require_container

require_container()

import ast
import unittest

from operations.checker.findings import assert_no_findings

from .guard_directives import documented_guard_indents
from .sources import ROOT, python_sources

_ESCAPE_PREFIX = "# repo-check: allow-literal-guard-error -- "


def _message_expressions(constructor: ast.Call) -> list[ast.expr]:
    """Use explicit message keywords or the message-first calling convention."""

    messages = [keyword.value for keyword in constructor.keywords if keyword.arg in {"message", "reason", "msg"}]

    if messages:
        return messages

    return constructor.args[:1]


def _literal_text(expression: ast.expr | None) -> bool:
    """Recognize string literals, their concatenation and f-strings without fields."""

    if isinstance(expression, ast.Constant):
        return isinstance(expression.value, str)

    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        return _literal_text(expression.left) and _literal_text(expression.right)

    if isinstance(expression, ast.JoinedStr):
        return not any(isinstance(part, ast.FormattedValue) for part in expression.values)

    return False


def guard_findings(*, source: str) -> list[int]:
    """Report direct raises with fixed messages, one finding per raise."""

    escaped_indents = documented_guard_indents(source, prefix=_ESCAPE_PREFIX)
    findings = []

    for guard in ast.walk(ast.parse(source)):
        if not isinstance(guard, ast.If):
            continue

        if escaped_indents.get(guard.lineno) == guard.col_offset:
            continue

        for rejection in [*guard.body, *guard.orelse]:
            if not isinstance(rejection, ast.Raise) or not isinstance(rejection.exc, ast.Call):
                continue

            if any(_literal_text(message) for message in _message_expressions(rejection.exc)):
                findings.append(rejection.lineno)

    return sorted(findings)


class GuardErrorEvidencePostureTests(unittest.TestCase):
    def test_fixed_guard_messages_are_flagged_without_other_conditions(self):
        constructors = [
            "ValueError('Fixed message')",
            "module.Error(message='Fixed message')",
            "Error(reason='Fixed message', cause=cause)",
            "Error(message=dynamic, reason='Fixed message')",
            "Error('Fixed message', code=Error.Code.INVALID)",
            "Error('Fixed ' + 'message')",
            "Error(f'Fixed message')",
        ]

        for constructor in constructors:
            for condition in ["invalid", "not ready", "True"]:
                with self.subTest(constructor=constructor, condition=condition):
                    source = (
                        f"if {condition}:\n"
                        f"    raise {constructor} from cause\n"
                    )

                    self.assertEqual([2], guard_findings(source=source))

    def test_direct_raises_after_work_and_in_else_branches_are_flagged(self):
        source = (
            "if invalid:\n"
            "    record_failure()\n"
            "    raise Error('Fixed message')\n"
            "else:\n"
            "    raise Error('Another fixed message')\n"
        )

        self.assertEqual([3, 5], guard_findings(source=source))

    def test_nonliteral_messages_and_existing_exceptions_are_outside_this_rule(self):
        for rejection in [
            "Error(f'Invalid {unrelated=}')",
            "Error(message)",
            "Error(message=f'{unrelated=}', code='fixed_code')",
            "existing_failure",
            "Error()",
        ]:
            with self.subTest(rejection=rejection):
                source = (
                    f"if invalid:\n"
                    f"    raise {rejection}\n"
                )

                self.assertEqual([], guard_findings(source=source))

    def test_documented_escape_covers_only_its_adjacent_guard(self):
        source = (
            "# repo-check: allow-literal-guard-error -- The protocol requires this exact reply\n"
            "if invalid:\n"
            "    raise Error('Fixed reply')\n"
            "if other_failure:\n"
            "    raise Error('Other fixed reply')\n"
        )

        self.assertEqual([5], guard_findings(source=source))

    def test_empty_distant_inline_and_string_escapes_do_not_apply(self):
        prefixes = [
            "# repo-check: allow-literal-guard-error --   \n",
            "# repo-check: allow-literal-guard-error -- Fixed by protocol\n"
            "\n",
            "'# repo-check: allow-literal-guard-error -- Fixed by protocol'\n",
            "marker = 1  # repo-check: allow-literal-guard-error -- Fixed by protocol\n",
            "    # repo-check: allow-literal-guard-error -- Fixed by protocol\n",
        ]

        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                source = prefix + (
                    "if invalid:\n"
                    "    raise Error('Fixed reply')\n"
                )

                self.assertEqual(1, len(guard_findings(source=source)))

    def test_authored_raising_guards_do_not_hide_their_state_in_literal_messages(self):
        findings = []

        for source_path in python_sources(production_only=False):
            for line in guard_findings(source=source_path.read_text(encoding="utf-8")):
                findings.append(
                    f"{source_path.relative_to(ROOT)}:{line}: literal-only guard error; show the failed state "
                    f"or document {_ESCAPE_PREFIX!r} with a concrete reason"
                )

        assert_no_findings(findings)
