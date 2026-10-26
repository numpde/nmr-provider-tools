# Port source: https://github.com/numpde/dapp32/blob/778c16f8d8b77181d2ac4cd11a62a2034ded2518/tests/checks/silent_defaults/test_python.py
# Original detector semantics and fixtures retained; inventories use authored NMRNet paths.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import io
import tokenize
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path

from ..sources import ROOT
from .test_shared_scanner import repo_files

ENV_NAME_FRAGMENTS = ("env", "environ", "process_env")
DEFAULTING_CALL_NAMES = {"get", "getenv", "pop", "setdefault"}
ARGPARSE_CALL_NAMES = {"add_argument"}


def python_files() -> list[Path]:
    return repo_files(("**/*.py",))


def is_env_mapping(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return any(fragment in node.id.lower() for fragment in ENV_NAME_FRAGMENTS)
    return (
        isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    )


def is_defaulting_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in DEFAULTING_CALL_NAMES:
        return False
    if node.func.attr == "getenv":
        return isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
    return is_env_mapping(node.func.value)


def is_env_subscript(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and is_env_mapping(node.value)


def is_explicit_defaulting_call(node: ast.AST) -> bool:
    if not is_defaulting_call(node):
        return False
    assert isinstance(node, ast.Call)
    if isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault":
        return True
    return len(node.args) >= 2 or any(keyword.arg == "default" for keyword in node.keywords)


def is_explicit_mapping_default_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in DEFAULTING_CALL_NAMES:
        return False
    if is_defaulting_call(node):
        return False
    if node.func.attr == "setdefault":
        return True
    return len(node.args) >= 2 or any(keyword.arg == "default" for keyword in node.keywords)


def contains_env_access(node: ast.AST) -> bool:
    return any(is_defaulting_call(child) or is_env_subscript(child) for child in ast.walk(node))


def is_env_validation_predicate(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return contains_env_access(node.operand) or is_env_validation_predicate(node.operand)
    # OR combines comparison predicates rather than supplying an input.
    # Nested defaults remain separate candidates; overloaded comparisons
    # and aliases are not resolved by this syntactic classification.
    return isinstance(node, ast.Compare)


def is_env_or_default_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and any(contains_env_access(value) for value in node.values)
        and any(not is_env_validation_predicate(value) for value in node.values)
    )


def is_absence_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in {"", None}


def is_default_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant | ast.List | ast.Tuple | ast.Dict | ast.Set)


def is_non_empty_default_value(node: ast.AST) -> bool:
    return not contains_env_access(node) and not is_absence_literal(node)


def is_env_default_if_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.IfExp)
        and not is_absence_literal(node.orelse)
        and (contains_env_access(node.body) or contains_env_access(node.test))
    )


def is_generic_or_default_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and not contains_env_access(node)
        and any(is_default_literal(value) for value in node.values)
    )


def is_generic_default_if_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.IfExp)
        and not contains_env_access(node)
        and (is_default_literal(node.body) or is_default_literal(node.orelse))
    )


def assigned_values_by_target(statements: list[ast.stmt]) -> dict[str, ast.AST]:
    values: dict[str, ast.AST] = {}
    for statement in statements:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                values[ast.dump(target)] = statement.value
        if isinstance(statement, ast.AnnAssign) and statement.value is not None:
            values[ast.dump(statement.target)] = statement.value
    return values


def has_same_target_fallback_assignment(
    primary_values: dict[str, ast.AST],
    fallback_values: dict[str, ast.AST],
    *,
    primary_predicate: Callable[[ast.AST], bool],
    fallback_predicate: Callable[[ast.AST], bool],
) -> bool:
    for target, primary_value in primary_values.items():
        fallback_value = fallback_values.get(target)
        if fallback_value is not None and primary_predicate(primary_value) and fallback_predicate(fallback_value):
            return True
    return False


def is_env_default_if_statement(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    body_values = assigned_values_by_target(node.body)
    else_values = assigned_values_by_target(node.orelse)
    return has_same_target_fallback_assignment(
        body_values,
        else_values,
        primary_predicate=contains_env_access,
        fallback_predicate=is_non_empty_default_value,
    ) or has_same_target_fallback_assignment(
        else_values,
        body_values,
        primary_predicate=contains_env_access,
        fallback_predicate=is_non_empty_default_value,
    )


def is_env_default_keyerror_handler(node: ast.AST) -> bool:
    if not isinstance(node, ast.Try):
        return False
    body_values = assigned_values_by_target(node.body)
    for handler in node.handlers:
        if not isinstance(handler.type, ast.Name) or handler.type.id != "KeyError":
            continue
        if has_same_target_fallback_assignment(
            body_values,
            assigned_values_by_target(handler.body),
            primary_predicate=contains_env_access,
            fallback_predicate=is_non_empty_default_value,
        ):
            return True
    return False


def is_argparse_default(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ARGPARSE_CALL_NAMES:
        return False
    return any(keyword.arg == "default" for keyword in node.keywords)


def is_argparse_optional_positional(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ARGPARSE_CALL_NAMES:
        return False
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return False
    if node.args[0].value.startswith("-"):
        return False
    return any(
        keyword.arg == "nargs" and isinstance(keyword.value, ast.Constant) and keyword.value.value == "?"
        for keyword in node.keywords
    )


def args_attribute_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "args":
        return node.attr
    return None


def args_none_compare(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Compare):
        return None
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Is):
        return None
    if len(node.comparators) != 1 or not isinstance(node.comparators[0], ast.Constant):
        return None
    if node.comparators[0].value is not None:
        return None
    return args_attribute_name(node.left)


def is_args_default_assignment(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    attr_name = args_none_compare(node.test)
    if attr_name is None:
        return False
    for statement in node.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if args_attribute_name(target) == attr_name:
                return True
    return False


def is_args_default_if_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.IfExp)
        and isinstance(node.test, ast.Name)
        and node.test.id == "args"
        and isinstance(node.body, ast.Subscript)
        and isinstance(node.body.value, ast.Name)
        and node.body.value.id == "args"
        and not is_absence_literal(node.orelse)
    )


def contains_raw_args_access(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name) and child.value.id == "args"
        for child in ast.walk(node)
    )


def is_args_default_if_statement(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    body_values = assigned_values_by_target(node.body)
    else_values = assigned_values_by_target(node.orelse)
    return has_same_target_fallback_assignment(
        body_values,
        else_values,
        primary_predicate=contains_raw_args_access,
        fallback_predicate=lambda node: not is_absence_literal(node),
    ) or has_same_target_fallback_assignment(
        else_values,
        body_values,
        primary_predicate=contains_raw_args_access,
        fallback_predicate=lambda node: not is_absence_literal(node),
    )


def is_silent_default(node: ast.AST) -> bool:
    return (
        is_explicit_defaulting_call(node)
        or is_explicit_mapping_default_call(node)
        or is_argparse_default(node)
        or is_argparse_optional_positional(node)
        or is_env_or_default_expression(node)
        or is_env_default_if_expression(node)
        or is_generic_or_default_expression(node)
        or is_generic_default_if_expression(node)
        or is_env_default_if_statement(node)
        or is_env_default_keyerror_handler(node)
        or is_args_default_assignment(node)
        or is_args_default_if_expression(node)
        or is_args_default_if_statement(node)
    )


def function_default_nodes(node: ast.AST) -> list[ast.AST]:
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return []

    return [
        default
        for default in [*node.args.defaults, *(default for default in node.args.kw_defaults if default is not None)]
        if not is_absence_literal(default)
    ]


def is_exception_fallback_handler(node: ast.AST) -> bool:
    if not isinstance(node, ast.ExceptHandler):
        return False

    for statement in node.body:
        if isinstance(statement, ast.Pass):
            return True
        if isinstance(statement, ast.Return) and statement.value is not None and not is_absence_literal(statement.value):
            return True

    return False


def source_snippet(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    return f"<{type(node).__name__}>" if segment is None else " ".join(segment.split())


def _default_candidates(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Keep raw detection independent of reviewed contract applicability."""
    for node in ast.walk(tree):
        if is_silent_default(node):
            yield node, "python silent default"
        for default in function_default_nodes(node):
            yield default, "python function default"
        if is_exception_fallback_handler(node):
            yield node, "python exception fallback"


def _default_finding(path: Path, source: str, node: ast.AST, label: str) -> str:
    return f"{path.relative_to(ROOT)}:{node.lineno}: {label}: {source_snippet(source, node)}"


def _has_boolean_env_input(node: ast.AST) -> bool:
    if is_defaulting_call(node) or is_env_subscript(node):
        return True
    if isinstance(node, ast.Compare):
        return contains_env_access(node)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _has_boolean_env_input(node.operand)
    return isinstance(node, ast.BoolOp) and any(_has_boolean_env_input(value) for value in node.values)


def _is_comparison_predicate(node: ast.AST) -> bool:
    if isinstance(node, ast.Compare):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _is_comparison_predicate(node.operand)
    return isinstance(node, ast.BoolOp) and all(_is_comparison_predicate(value) for value in node.values)


def _boolean_env_or_guards(node: ast.AST) -> Iterator[ast.AST]:
    # Only BoolOp/Not edges preserve consumption as the If's branch truth.
    # Calls, comparisons and other value consumers stop this traversal.
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.Or) and all(_has_boolean_env_input(value) for value in node.values):
            yield node
        for value in node.values:
            yield from _boolean_env_or_guards(value)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield from _boolean_env_or_guards(node.operand)


def python_default_findings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    return [_default_finding(path, source, node, label) for node, label in _default_candidates(tree)]


def _discarded_env_pop_contract(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "pop"
        and is_env_mapping(call.func.value)
        and len(call.args) == 2
        and not call.keywords
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value is None
    )


def _typed_pass_contract(handler: ast.ExceptHandler, protected: ast.Try) -> bool:
    if len(handler.body) != 1 or not isinstance(handler.body[0], ast.Pass):
        return False
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    if not types:
        return False
    broad = {"Exception", "BaseException", "ExceptionGroup", "BaseExceptionGroup"}
    for declared in types:
        reference = declared
        while isinstance(reference, ast.Attribute):
            if reference.attr in broad:
                return False
            reference = reference.value
        if not isinstance(reference, ast.Name) or reference.id in broad:
            return False
    # A Pass mentions no input itself; inspect what its protected suite can
    # swallow. Reviewers judge type semantics; aliases are not resolved here.
    return not any(
        contains_env_access(statement)
        or contains_raw_args_access(statement)
        or any(
            args_attribute_name(child) is not None
            or (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and (child.func.attr in ARGPARSE_CALL_NAMES or child.func.attr in {"parse_args", "parse_known_args"})
            )
            for child in ast.walk(statement)
        )
        for statement in protected.body
    )


def _contract_candidate(node: ast.AST, label: str, *, protected: ast.Try | None = None) -> bool:
    # An annotation records a reviewed consumer contract, not inferred safety.
    if (
        contains_env_access(node)
        or contains_raw_args_access(node)
        or any(args_attribute_name(child) is not None for child in ast.walk(node))
    ):
        return False
    if label == "python exception fallback":
        return isinstance(node, ast.ExceptHandler) and protected is not None and _typed_pass_contract(node, protected)
    if label not in {"python silent default", "python function default"}:
        return False
    if label == "python function default":
        # A nested callable's body has no signature annotation of its own.
        return not any(
            isinstance(child, ast.Lambda) or is_argparse_default(child) or is_argparse_optional_positional(child)
            for child in ast.walk(node)
        )
    return (
        is_explicit_mapping_default_call(node) or is_generic_or_default_expression(node) or is_generic_default_if_expression(node)
    )


def _positive_exit(value):
    if isinstance(value, ast.Constant):
        return type(value.value) is int and value.value > 0
    return isinstance(value, ast.IfExp) and _positive_exit(value.body) and _positive_exit(value.orelse)


def _exception_outcome_contract(handler, protected):
    # This is annotation applicability, not a proof of exception semantics or
    # reporting. Reviewers own those claims. Never cover nested input defaults.
    if handler.type is None:
        return False
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Try, ast.With,
                             ast.AsyncWith, ast.For, ast.AsyncFor, ast.While))
           for statement in handler.body for node in ast.walk(statement)):
        return False
    if any(contains_env_access(statement) or contains_raw_args_access(statement) for statement in protected.body):
        return False
    if isinstance(handler.body[-1], ast.Return) and _positive_exit(handler.body[-1].value):
        # A direct final return prevents a swallowed failure from falling through
        # to the process success path. Earlier conditional returns must also fail.
        return all(_positive_exit(node.value) for statement in handler.body
                   for node in ast.walk(statement) if isinstance(node, ast.Return))
    declared = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    if any(not isinstance(node, ast.Name) or node.id in {"Exception", "BaseException", "ExceptionGroup", "BaseExceptionGroup"}
           for node in declared):
        return False
    if any(contains_env_access(statement) or contains_raw_args_access(statement)
           or any(args_attribute_name(node) is not None for node in ast.walk(statement)) for statement in protected.body):
        return False
    if len(handler.body) != 1 or not isinstance(handler.body[0], ast.Return):
        return False
    value = handler.body[0].value
    refusal = isinstance(value, ast.Constant) and value.value is False
    unavailable = (
        isinstance(value, ast.Dict) and len(value.keys) == 1 and isinstance(value.keys[0], ast.Constant)
        and value.keys[0].value == "detail_unavailable" and isinstance(value.values[0], ast.Constant)
        and isinstance(value.values[0].value, str) and bool(value.values[0].value.strip())
    )
    return refusal or unavailable or isinstance(value, ast.Name)


def python_operator_default_findings(path: Path) -> list[str]:
    """Apply boolean guard classification and local reviewed exceptions.

    Raw candidates stay unchanged. Independent ENV activation ORs consumed only
    as If branch truth admit their own finding, never nested defaults or bodies.
    OR trees with only comparison leaves through BoolOp/Not edges admit their
    silent-default finding in any consumer; other labels and nested defaults stay strict.

    Single-line statements, If conditions and For iterables cover one generic expression and its
    generic default candidates. With context expressions, assignment and return values,
    and a standalone Call expression may span lines; bodies and targets remain outside
    their boundary. The Call region includes its callee, receiver and arguments;
    it does not infer purity or cover other multiline Expr values.
    A dedicated comment before a literal Dict key covers only that entry's
    value under the same outermost-expression and no-lambda guards. Its Dict
    must contain only literal keys, with one key AST anchored on that next line.
    The dedicated allow-lambda-body tag immediately before a parenthesized
    lambda result requires an explanation and covers only a generic default
    candidate. Signature defaults, surrounding expressions and sibling lambdas
    remain outside its boundary; nested lambdas and operator inputs are rejected.
    These regions cover one outermost generic candidate and its generic children,
    never a nested callable signature. Before a callable definition, it covers
    only signature defaults, including multiline signatures. Decorators, type
    annotations and the body remain outside that boundary. An annotation before
    a typed, Pass-only except header covers just that handler; operator access
    in its protected Try suite remains strict. The existing allow-pop-default tag
    covers only a discarded fixed-key ENV pop(None), using the same explanation
    requirement. The legacy production ENV-default rule remains separate and strict.
    An allow-exception-outcome tag covers only a typed direct refusal, retained named result,
    unavailable-detail record, or handler with a direct positive final result. It does not
    prove the named result is a failure or that diagnostics are safe; review owns those claims.
    Each tag requires a nonblank explanation; reviewers judge its meaning.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    candidates = list(_default_candidates(tree))
    accepted = {
        guard for statement in ast.walk(tree) if isinstance(statement, ast.If) for guard in _boolean_env_or_guards(statement.test)
    }
    findings: list[str] = []
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)]
    entries = [
        (key, value, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
    ]
    handlers = [(handler, owner) for owner in ast.walk(tree) if isinstance(owner, ast.Try) for handler in owner.handlers]
    lambda_bodies = [node.body for node in ast.walk(tree) if isinstance(node, ast.Lambda)]
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT or not token.string.startswith(
            ("# silent-defaults: allow-contract", "# silent-defaults: allow-pop-default", "# silent-defaults: allow-lambda-body", "# silent-defaults: allow-exception-outcome")
        ):
            continue
        prefix, separator, reason = token.string.partition(" -- ")
        next_line = token.end[0] + 1
        following = [node for node in statements if node.lineno == next_line]
        following_loops = [node for node in following if isinstance(node, ast.For)]
        following_handlers = [(handler, owner) for handler, owner in handlers if handler.lineno == next_line]
        following_entries = [(key, value, owner) for key, value, owner in entries if key is not None and key.lineno == next_line]
        protected = None
        if prefix == "# silent-defaults: allow-exception-outcome":
            if (separator and not token.line[:token.start[1]].strip() and len(following_handlers) == 1
                    and following_handlers[0][0].col_offset == token.start[1] and reason.strip()
                    and _exception_outcome_contract(*following_handlers[0])):
                accepted.add(following_handlers[0][0])
            else:
                findings.append(f"{path.relative_to(ROOT)}:{token.start[0]}: exception-outcome annotation requires an explanation and an immediate bounded failure outcome")
            continue
        if prefix == "# silent-defaults: allow-lambda-body":
            # This placement owns one callable result, never its signature or container.
            following = [body for body in lambda_bodies if body.lineno == next_line]
            selected = [(node, label) for node, label in candidates if len(following) == 1 and node in ast.walk(following[0])]
            boundary = (
                len(following) == 1
                and (following[0], "python silent default") in selected
                and all(label == "python silent default" for _, label in selected)
                and not any(
                    isinstance(child, ast.Lambda) or is_argparse_default(child) or is_argparse_optional_positional(child)
                    for child in ast.walk(following[0])
                )
            )
        elif prefix == "# silent-defaults: allow-pop-default":
            selected = [
                (node, label)
                for node, label in candidates
                if len(following) == 1 and isinstance(following[0], ast.Expr) and node is following[0].value
            ]
            boundary = len(following) == len(selected) == 1 and _discarded_env_pop_contract(following[0])
        elif following_handlers:
            # ExceptHandler is not ast.stmt. Its header owns this placement,
            # including a same-line Pass; no body or sibling is accepted.
            following = [handler for handler, _owner in following_handlers]
            selected = [(node, label) for node, label in candidates if node in following]
            boundary = len(following_handlers) == len(selected) == 1
            if boundary:
                protected = following_handlers[0][1]
        elif not following and following_entries:
            following = [key for key, _value, _owner in following_entries]
            selected = [
                (node, label)
                for node, label in candidates
                if any(node in ast.walk(value) for _key, value, _owner in following_entries)
            ]
            outermost = [node for node, _ in selected if all(other in ast.walk(node) for other, _ in selected)]
            boundary = (
                len(following_entries) == 1
                and all(isinstance(key, ast.Constant) for key in following_entries[0][2].keys)
                and len(outermost) == 1
                and all(label == "python silent default" for _, label in selected)
                and not any(
                    isinstance(child, ast.Lambda) for _key, value, _owner in following_entries for child in ast.walk(value)
                )
            )
        elif len(following_loops) == 1:
            # A For header owns only its iterable, even with a same-line body.
            # Targets, body and else suites retain their independent findings.
            following = following_loops
            expression = following[0].iter
            selected = [(node, label) for node, label in candidates if node in ast.walk(expression)]
            outermost = [node for node, _ in selected if all(other in ast.walk(node) for other, _ in selected)]
            boundary = (
                expression.end_lineno == next_line
                and len(outermost) == 1
                and all(label == "python silent default" for _, label in selected)
                and not any(isinstance(child, ast.Lambda) for child in ast.walk(expression))
            )
        elif len(following) == 1 and isinstance(following[0], ast.FunctionDef | ast.AsyncFunctionDef):
            defaults = function_default_nodes(following[0])
            selected = [(node, label) for node, label in candidates if any(node in ast.walk(default) for default in defaults)]
            boundary = bool(defaults)
        elif len(following) == 1 and (
            isinstance(following[0], ast.With | ast.Assign | ast.Return)
            or (isinstance(following[0], ast.Expr) and isinstance(following[0].value, ast.Call))
        ):
            statement = following[0]
            expressions = (
                [item.context_expr for item in statement.items]
                if isinstance(statement, ast.With)
                else ([] if statement.value is None else [statement.value])
            )
            selected = [
                (node, label) for node, label in candidates if any(node in ast.walk(expression) for expression in expressions)
            ]
            outermost = [node for node, _ in selected if all(other in ast.walk(node) for other, _ in selected)]
            boundary = (
                len(outermost) == 1
                and all(label == "python silent default" for _, label in selected)
                and not any(isinstance(child, ast.Lambda) for expression in expressions for child in ast.walk(expression))
            )
        else:
            selected = [(node, label) for node, label in candidates if node.lineno == next_line]
            # Nested defaults may express one contract; sibling expressions do
            # not. Strict candidates must still pass their own gate.
            outermost = [node for node, _ in selected if all(other in ast.walk(node) for other, _ in selected)]
            boundary = (
                len(following) == 1
                and (
                    following[0].end_lineno == next_line
                    or (isinstance(following[0], ast.If) and following[0].test.end_lineno == next_line)
                )
                and len(outermost) == 1
                and all(node.end_lineno == next_line for node, _ in selected)
            )
        if (
            prefix
            in {
                "# silent-defaults: allow-contract",
                "# silent-defaults: allow-pop-default",
                "# silent-defaults: allow-lambda-body",
            }
            and separator
            and reason.strip()
            and not token.line[: token.start[1]].strip()
            and len(following) == 1
            and following[0].col_offset == token.start[1]
            and boundary
            # Only the pop-specific boundary admits this discarded ENV access;
            # every ordinary contract keeps the shared operator/header guards.
            and (
                prefix == "# silent-defaults: allow-pop-default"
                or all(_contract_candidate(node, label, protected=protected) for node, label in selected)
            )
        ):
            accepted.update(node for node, _ in selected)
        else:
            if prefix == "# silent-defaults: allow-lambda-body":
                requirement = "an explanation and one immediate generic lambda result at the same column, without nested lambdas or operator inputs"
            else:
                requirement = "an explanation and one immediate generic expression, literal-key Dict value, callable signature, typed Pass-only handler or discarded ENV pop(None)"
            findings.append(f"{path.relative_to(ROOT)}:{token.start[0]}: python contract annotation requires {requirement}")
    findings.extend(
        _default_finding(path, source, node, label)
        for node, label in candidates
        if node not in accepted
        and not (
            label == "python silent default"
            and isinstance(node, ast.BoolOp)
            and isinstance(node.op, ast.Or)
            and _is_comparison_predicate(node)
        )
    )
    return findings


class PythonSilentDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_matchers_flag_representative_python_defaults(self) -> None:
        self.assertTrue(python_source_has_default('value = os.environ.get("PORT", "8080")'))
        self.assertTrue(python_source_has_default('value = config.get("ports", [])'))
        self.assertTrue(python_source_has_default('value = config.pop("profile", "demo")'))
        self.assertTrue(python_source_has_default('config.setdefault("profile", "demo")'))
        self.assertTrue(python_source_has_default('value = os.environ["PORT"].strip() or "8080"'))
        self.assertTrue(python_source_has_default("port = parsed.port or 443"))
        self.assertTrue(python_source_has_default('path = parsed.path or "/"'))
        self.assertTrue(python_source_has_default('manager = value if value is not None else "apk"'))
        self.assertTrue(python_source_has_default('value = env["PORT"] if "PORT" in env else "8080"'))
        self.assertTrue(python_source_has_default('if "PORT" in env:\n    port = env["PORT"]\nelse:\n    port = "8080"'))
        self.assertTrue(python_source_has_default('try:\n    port = env["PORT"]\nexcept KeyError:\n    port = "8080"'))
        self.assertTrue(python_source_has_default('parser.add_argument("--port", default="8080")'))
        self.assertTrue(python_source_has_default('parser.add_argument("profile", nargs="?")'))
        self.assertTrue(python_source_has_default('def run(port: str = "8080"):\n    pass'))
        self.assertTrue(python_source_has_default("try:\n    run()\nexcept Exception:\n    pass"))
        self.assertTrue(python_source_has_default("try:\n    run()\nexcept Exception:\n    return []"))
        self.assertTrue(python_source_has_default('if args.port is None:\n    args.port = "8080"'))
        self.assertTrue(python_source_has_default('profile = args[0] if args else "all"'))
        self.assertTrue(python_source_has_default('if args:\n    profile = args[0]\nelse:\n    profile = "all"'))

    def test_matchers_ignore_explicit_absence(self) -> None:
        self.assertFalse(python_source_has_default("value = default if raw is None else raw"))
        self.assertFalse(python_source_has_default('value = config.get("profile")'))
        self.assertFalse(python_source_has_default("def run(optional: str | None = None):\n    pass"))
        self.assertFalse(
            python_source_has_default('try:\n    run()\nexcept Exception as exc:\n    raise RuntimeError("failed") from exc')
        )
        self.assertFalse(python_source_has_default('optional = env["OPTIONAL"] if "OPTIONAL" in env else ""'))
        self.assertFalse(python_source_has_default('optional = env["OPTIONAL"] if "OPTIONAL" in env else None'))

    def test_python_file_scan_is_repository_wide(self) -> None:
        scanned = {path.relative_to(ROOT).as_posix() for path in python_files()}

        self.assertIn("tools/container_guard.py", scanned)
        self.assertIn("tests/repository/quality/sources.py", scanned)
        self.assertIn("tests/repository/test_conventions.py", scanned)

    def test_python_files_do_not_default_operator_inputs(self) -> None:
        findings: list[str] = []
        for path in python_files():
            findings.extend(python_operator_default_findings(path))

        self.assertEqual([], findings)


def python_source_has_default(source: str) -> bool:
    tree = ast.parse(source)
    return any(
        is_silent_default(node) or function_default_nodes(node) or is_exception_fallback_handler(node) for node in ast.walk(tree)
    )


if __name__ == "__main__":
    unittest.main()
