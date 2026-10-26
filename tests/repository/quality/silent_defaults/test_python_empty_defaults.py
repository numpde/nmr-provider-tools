# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/test_python_empty_defaults.py
# Complete original detector assertion groups; project input paths adapted locally.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import unittest
from collections.abc import Callable
from pathlib import Path

from ._repo_surfaces import repo_source, surface_files

PYTHON_SURFACE_GLOBS = (
    "*.py",
    "tools/**/*.py",
    "containers/**/*.py",
    "src/**/*.py",
    "packages/*/src/**/*.py",
)
PYTHON_SCAN_EXCLUDED_PARTS = frozenset(
    {
        "tests",
    }
)

ENV_NAME_FRAGMENTS = ("env", "environ")
ARGPARSE_CALL_NAMES = {"add_argument"}
DEFAULTING_CALL_NAMES = {
    "get",
    "getenv",
    "pop",
    "setdefault",
}
ENV_MODULE_NAMES = {"os"}
EMPTY_GET_DEFAULT_WAIVER = "silent-defaults: allow-empty-get"
POP_DEFAULT_WAIVER = "silent-defaults: allow-pop-default"


def _python_operator_files() -> list[Path]:
    return surface_files(
        PYTHON_SURFACE_GLOBS,
        extra_exclude_parts=PYTHON_SCAN_EXCLUDED_PARTS,
    )


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id in ENV_MODULE_NAMES
    )


def _is_env_mapping(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and any(fragment in node.id.lower() for fragment in ENV_NAME_FRAGMENTS)) or _is_os_environ(
        node
    )


def _source_snippet(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        return f"<{type(node).__name__}>"
    return " ".join(segment.split())


def _finding(path: Path, source: str, node: ast.AST) -> str:
    return f"{repo_source(path)}:{node.lineno}: {_source_snippet(source, node)}"


def _is_env_subscript(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and _is_env_mapping(node.value)


def _is_defaulting_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in DEFAULTING_CALL_NAMES:
        return False
    if node.func.attr == "getenv":
        return isinstance(node.func.value, ast.Name) and node.func.value.id in ENV_MODULE_NAMES
    if _is_env_mapping(node.func.value):
        return True
    if _is_os_environ(node.func.value):
        return True
    return isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "environ"


def _is_explicit_env_default_call(node: ast.AST) -> bool:
    if not _is_defaulting_call(node):
        return False
    call = node
    assert isinstance(call, ast.Call)
    if isinstance(call.func, ast.Attribute) and call.func.attr == "setdefault":
        return True
    if len(call.args) >= 2:
        return True
    return any(keyword.arg == "default" for keyword in call.keywords)


def _contains_env_access(node: ast.AST) -> bool:
    if _is_defaulting_call(node):
        return True
    if _is_env_subscript(node):
        return True
    return any(_is_defaulting_call(child) or _is_env_subscript(child) for child in ast.walk(node))


def _is_env_validation_predicate(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _contains_env_access(node.operand) or _is_env_validation_predicate(node.operand)
    if isinstance(node, ast.Compare):
        return any(_is_env_mapping(comparator) for comparator in node.comparators)
    return False


def _is_env_or_default_expression(node: ast.BoolOp) -> bool:
    return any(_contains_env_access(value) for value in node.values) and any(
        not _is_env_validation_predicate(value) for value in node.values
    )


def _is_empty_absence_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in {"", None}


def _is_non_empty_default_value(node: ast.AST) -> bool:
    return not _contains_env_access(node) and not _is_empty_absence_literal(node)


def _is_not_absence_literal(node: ast.AST) -> bool:
    return not _is_empty_absence_literal(node)


def _is_falsy_default_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None or node.value is False or node.value == "" or node.value == 0
    if isinstance(node, ast.Dict):
        return len(node.keys) == 0
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return len(node.elts) == 0
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in {"dict", "list", "set", "tuple"} and not node.args and not node.keywords
    return False


def _is_falsy_default_boolop(node: ast.AST) -> bool:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    return any(_is_falsy_default_literal(value) for value in node.values[1:])


def _is_empty_get_default_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "get" or len(node.args) < 2:
        return False
    return _is_falsy_default_literal(node.args[1])


def _is_pop_default_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "pop" or len(node.args) < 2:
        return False
    return _is_falsy_default_literal(node.args[1])


def _is_zero_getattr_default_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return False
    if node.func.id != "getattr" or len(node.args) < 3:
        return False
    default = node.args[2]
    return isinstance(default, ast.Constant) and default.value == 0 and default.value is not False


def _has_empty_get_waiver(source_lines: list[str], node: ast.AST) -> bool:
    start = max(node.lineno - 2, 0)
    end = max(getattr(node, "end_lineno", node.lineno), node.lineno)
    return any(EMPTY_GET_DEFAULT_WAIVER in line for line in source_lines[start:end])


def _has_pop_default_waiver(source_lines: list[str], node: ast.AST) -> bool:
    start = max(node.lineno - 2, 0)
    end = max(getattr(node, "end_lineno", node.lineno), node.lineno)
    return any(POP_DEFAULT_WAIVER in line for line in source_lines[start:end])


def _is_env_default_if_expression(node: ast.AST) -> bool:
    if not isinstance(node, ast.IfExp):
        return False
    if _is_empty_absence_literal(node.orelse):
        return False
    return _contains_env_access(node.body) or _contains_env_access(node.test)


def _assigned_values_by_target(statements: list[ast.stmt]) -> dict[str, ast.AST]:
    values: dict[str, ast.AST] = {}
    for statement in statements:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                values[ast.dump(target)] = statement.value
        if isinstance(statement, ast.AnnAssign) and statement.value is not None:
            values[ast.dump(statement.target)] = statement.value
    return values


def _has_same_target_fallback_assignment(
    primary_values: dict[str, ast.AST],
    fallback_values: dict[str, ast.AST],
    *,
    primary_predicate: Callable[[ast.AST], bool],
    fallback_predicate: Callable[[ast.AST], bool],
) -> bool:
    for target, primary_value in primary_values.items():
        fallback_value = fallback_values.get(target)
        if fallback_value is None:
            continue
        if primary_predicate(primary_value) and fallback_predicate(fallback_value):
            return True
    return False


def _is_env_default_if_statement(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    body_values = _assigned_values_by_target(node.body)
    else_values = _assigned_values_by_target(node.orelse)
    return _has_same_target_fallback_assignment(
        body_values,
        else_values,
        primary_predicate=_contains_env_access,
        fallback_predicate=_is_non_empty_default_value,
    ) or _has_same_target_fallback_assignment(
        else_values,
        body_values,
        primary_predicate=_contains_env_access,
        fallback_predicate=_is_non_empty_default_value,
    )


def _is_env_default_keyerror_handler(node: ast.AST) -> bool:
    if not isinstance(node, ast.Try):
        return False
    body_values = _assigned_values_by_target(node.body)
    for handler in node.handlers:
        if not isinstance(handler.type, ast.Name) or handler.type.id != "KeyError":
            continue
        if _has_same_target_fallback_assignment(
            body_values,
            _assigned_values_by_target(handler.body),
            primary_predicate=_contains_env_access,
            fallback_predicate=_is_non_empty_default_value,
        ):
            return True
    return False


def _is_argparse_default_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ARGPARSE_CALL_NAMES:
        return False
    return any(keyword.arg == "default" for keyword in node.keywords)


def _is_argparse_optional_positional(node: ast.AST) -> bool:
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


def _args_attribute_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "args":
        return node.attr
    return None


def _contains_args_attribute_access(node: ast.AST) -> bool:
    return any(_args_attribute_name(child) is not None for child in ast.walk(node))


def _is_literal_non_absence_default(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value not in {"", None}


def _is_args_attribute_or_default_expression(node: ast.AST) -> bool:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    return _contains_args_attribute_access(node.values[0]) and any(
        _is_literal_non_absence_default(value) for value in node.values[1:]
    )


def _is_args_none_compare(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Compare):
        return None
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Is):
        return None
    if len(node.comparators) != 1 or not isinstance(node.comparators[0], ast.Constant):
        return None
    if node.comparators[0].value is not None:
        return None
    return _args_attribute_name(node.left)


def _is_args_default_assignment(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    attr_name = _is_args_none_compare(node.test)
    if attr_name is None:
        return False
    for statement in node.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if _args_attribute_name(target) == attr_name:
                return True
    return False


def _is_args_default_if_expression(node: ast.AST) -> bool:
    if not isinstance(node, ast.IfExp):
        return False
    if not isinstance(node.test, ast.Name) or node.test.id != "args":
        return False
    if not isinstance(node.body, ast.Subscript):
        return False
    if not isinstance(node.body.value, ast.Name) or node.body.value.id != "args":
        return False
    return not (isinstance(node.orelse, ast.Constant) and node.orelse.value is None)


def _contains_raw_args_access(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name) and child.value.id == "args"
        for child in ast.walk(node)
    )


def _is_args_default_if_statement(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    body_values = _assigned_values_by_target(node.body)
    else_values = _assigned_values_by_target(node.orelse)
    return _has_same_target_fallback_assignment(
        body_values,
        else_values,
        primary_predicate=_contains_raw_args_access,
        fallback_predicate=_is_not_absence_literal,
    ) or _has_same_target_fallback_assignment(
        else_values,
        body_values,
        primary_predicate=_contains_raw_args_access,
        fallback_predicate=_is_not_absence_literal,
    )


def _env_default_findings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_explicit_env_default_call(node):
            findings.append(_finding(path, source, node))
        if _is_argparse_default_call(node):
            findings.append(_finding(path, source, node))
        if _is_argparse_optional_positional(node):
            findings.append(_finding(path, source, node))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and _is_env_or_default_expression(node):
            findings.append(_finding(path, source, node))
        if _is_env_default_if_expression(node):
            findings.append(_finding(path, source, node))
        if _is_env_default_if_statement(node):
            findings.append(_finding(path, source, node))
        if _is_env_default_keyerror_handler(node):
            findings.append(_finding(path, source, node))
        if _is_args_default_assignment(node):
            findings.append(_finding(path, source, node))
        if _is_args_attribute_or_default_expression(node):
            findings.append(_finding(path, source, node))
        if _is_args_default_if_expression(node):
            findings.append(_finding(path, source, node))
        if _is_args_default_if_statement(node):
            findings.append(_finding(path, source, node))
    return findings


def _falsy_default_findings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if _is_falsy_default_boolop(node):
            findings.append(_finding(path, source, node))
    return findings


def _empty_get_default_findings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if _is_empty_get_default_call(node) and not _has_empty_get_waiver(source_lines, node):
            findings.append(_finding(path, source, node))
    return findings


def _zero_getattr_default_findings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if _is_zero_getattr_default_call(node):
            findings.append(_finding(path, source, node))
    return findings


def _pop_default_findings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if _is_pop_default_call(node) and not _has_pop_default_waiver(source_lines, node):
            findings.append(_finding(path, source, node))
    return findings


def _has_env_default_expression(source: str) -> bool:
    tree = ast.parse(source, filename="<inline>")
    return any(
        (isinstance(node, ast.Call) and _is_explicit_env_default_call(node))
        or _is_argparse_default_call(node)
        or _is_argparse_optional_positional(node)
        or (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and _is_env_or_default_expression(node))
        or _is_env_default_if_expression(node)
        or _is_env_default_if_statement(node)
        or _is_env_default_keyerror_handler(node)
        or _is_args_default_assignment(node)
        or _is_args_attribute_or_default_expression(node)
        or _is_args_default_if_expression(node)
        or _is_args_default_if_statement(node)
        for node in ast.walk(tree)
    )


def _has_falsy_default_expression(source: str) -> bool:
    tree = ast.parse(source, filename="<inline>")
    return any(_is_falsy_default_boolop(node) for node in ast.walk(tree))


def _has_empty_get_default_expression(source: str) -> bool:
    tree = ast.parse(source, filename="<inline>")
    source_lines = source.splitlines()
    return any(_is_empty_get_default_call(node) and not _has_empty_get_waiver(source_lines, node) for node in ast.walk(tree))


def _has_zero_getattr_default_expression(source: str) -> bool:
    tree = ast.parse(source, filename="<inline>")
    return any(_is_zero_getattr_default_call(node) for node in ast.walk(tree))


def _has_pop_default_expression(source: str) -> bool:
    tree = ast.parse(source, filename="<inline>")
    source_lines = source.splitlines()
    return any(_is_pop_default_call(node) and not _has_pop_default_waiver(source_lines, node) for node in ast.walk(tree))


class PythonEmptyDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_python_matcher_flags_env_default_expression(self) -> None:
        self.assertTrue(_has_env_default_expression('network = os.getenv("MIGRATE_DOCKER_NETWORK", default_network)'))
        self.assertTrue(_has_env_default_expression('network = env["MIGRATE_DOCKER_NETWORK"].strip() or default_network'))
        self.assertTrue(_has_env_default_expression('network = process_env.get("MIGRATE_DOCKER_NETWORK", default_network)'))
        self.assertTrue(_has_env_default_expression('mode = env.get("VOTE_MCP_ORAS_MODE") or "docker"'))
        self.assertTrue(_has_env_default_expression('env.setdefault("OCI_NAMESPACE", "numpde/vote-mcp")'))
        self.assertTrue(_has_env_default_expression('branch = env["DEPLOY_BRANCH"] if "DEPLOY_BRANCH" in env else "main"'))
        self.assertTrue(
            _has_env_default_expression(
                'path = env["DEPLOY_LOCK_FILE"].strip() if "DEPLOY_LOCK_FILE" in env and env["DEPLOY_LOCK_FILE"].strip() else default_path'
            )
        )
        self.assertTrue(
            _has_env_default_expression(
                'if "DEPLOY_BRANCH" in env:\n    branch = env["DEPLOY_BRANCH"]\nelse:\n    branch = "main"'
            )
        )
        self.assertTrue(
            _has_env_default_expression('try:\n    branch = env["DEPLOY_BRANCH"]\nexcept KeyError:\n    branch = "main"')
        )
        self.assertTrue(_has_env_default_expression('parser.add_argument("--profile", default="demo")'))
        self.assertTrue(_has_env_default_expression('parser.add_argument("profile", nargs="?")'))
        self.assertTrue(_has_env_default_expression('if args.profile is None:\n    args.profile = "demo"'))
        self.assertTrue(_has_env_default_expression('profile = args.profile or "demo"'))
        self.assertTrue(_has_env_default_expression('profile = args[0] if args else "all"'))
        self.assertTrue(_has_env_default_expression('if args:\n    profile = args[0]\nelse:\n    profile = "all"'))

    def test_python_matcher_ignores_explicit_none_check(self) -> None:
        self.assertFalse(_has_env_default_expression("value = default if raw is None else raw"))
        self.assertFalse(_has_env_default_expression('if "KEY" not in env or not env["KEY"]: raise ValueError("KEY")'))
        self.assertFalse(_has_env_default_expression('optional = env["OPTIONAL_REF"] if "OPTIONAL_REF" in env else ""'))
        self.assertFalse(_has_env_default_expression('optional = env["OPTIONAL_REF"] if "OPTIONAL_REF" in env else None'))
        self.assertFalse(
            _has_env_default_expression('if "OPTIONAL_REF" in env:\n    optional = env["OPTIONAL_REF"]\nelse:\n    optional = ""')
        )
        self.assertFalse(_has_env_default_expression("enabled = args.enabled or args.force_enabled"))

    def test_python_matcher_flags_falsy_empty_default_expressions(self) -> None:
        self.assertTrue(_has_falsy_default_expression("payload = payload or {}"))
        self.assertTrue(_has_falsy_default_expression("items = items or []"))
        self.assertTrue(_has_falsy_default_expression('name = name or ""'))
        self.assertTrue(_has_falsy_default_expression("count = count or 0"))
        self.assertTrue(_has_falsy_default_expression("enabled = enabled or False"))
        self.assertTrue(_has_falsy_default_expression("extra = dict(extra or {})"))
        self.assertTrue(_has_falsy_default_expression("headers = headers or dict()"))

    def test_python_matcher_accepts_explicit_absence_checks(self) -> None:
        self.assertFalse(_has_falsy_default_expression("payload = {} if payload is None else payload"))
        self.assertFalse(_has_falsy_default_expression("name = '' if name is None else name"))
        self.assertFalse(_has_falsy_default_expression("if payload is None:\n    payload = {}\nelse:\n    payload = payload"))
        self.assertFalse(_has_falsy_default_expression("raise RuntimeError('bad') or value"))

    def test_python_matcher_flags_unannotated_empty_get_defaults(self) -> None:
        self.assertTrue(_has_empty_get_default_expression('payload.get("context", {})'))
        self.assertTrue(_has_empty_get_default_expression('payload.get("items", [])'))
        self.assertTrue(_has_empty_get_default_expression('headers.get("Allow", "")'))
        self.assertTrue(_has_empty_get_default_expression('payload.get("value", None)'))

    def test_python_matcher_accepts_annotated_empty_get_defaults(self) -> None:
        self.assertFalse(
            _has_empty_get_default_expression(
                'payload.get("context", {})  # silent-defaults: allow-empty-get -- protocol absence'
            )
        )
        self.assertFalse(
            _has_empty_get_default_expression(
                '# silent-defaults: allow-empty-get -- protocol absence\npayload.get("context", {})'
            )
        )
        self.assertFalse(
            _has_empty_get_default_expression(
                'payload.get(\n    "context",\n    {},  # silent-defaults: allow-empty-get -- protocol absence\n)'
            )
        )

    def test_python_matcher_flags_zero_getattr_defaults(self) -> None:
        self.assertTrue(_has_zero_getattr_default_expression('returncode = getattr(result, "returncode", 0)'))
        self.assertTrue(_has_zero_getattr_default_expression('attempts = getattr(config, "attempts", 0)'))
        self.assertFalse(_has_zero_getattr_default_expression("returncode = result.returncode"))
        self.assertFalse(_has_zero_getattr_default_expression('value = getattr(result, "value", None)'))

    def test_python_matcher_flags_unannotated_pop_defaults(self) -> None:
        self.assertTrue(_has_pop_default_expression('payload.pop("secret", None)'))
        self.assertTrue(_has_pop_default_expression('headers.pop("X-Robots-Tag", "")'))
        self.assertFalse(
            _has_pop_default_expression(
                '# silent-defaults: allow-pop-default -- delete if present\nheaders.pop("X-Robots-Tag", None)'
            )
        )

    def test_python_operator_surfaces_do_not_default_env_reads(self) -> None:
        findings: list[str] = []
        for path in _python_operator_files():
            findings.extend(_env_default_findings(path))

        self.assertEqual([], findings)

    def test_python_operator_surfaces_do_not_use_falsy_empty_defaults(self) -> None:
        findings: list[str] = []
        for path in _python_operator_files():
            findings.extend(_falsy_default_findings(path))

        self.assertEqual([], findings)

    def test_python_operator_surfaces_annotate_empty_get_defaults(self) -> None:
        findings: list[str] = []
        for path in _python_operator_files():
            findings.extend(_empty_get_default_findings(path))

        self.assertEqual([], findings)

    def test_python_operator_surfaces_do_not_default_getattr_to_zero(self) -> None:
        findings: list[str] = []
        for path in _python_operator_files():
            findings.extend(_zero_getattr_default_findings(path))

        self.assertEqual([], findings)

    def test_python_operator_surfaces_annotate_pop_defaults(self) -> None:
        findings: list[str] = []
        for path in _python_operator_files():
            findings.extend(_pop_default_findings(path))

        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
