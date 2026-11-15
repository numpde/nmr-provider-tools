"""Identify genuine debug fields without trusting manually written labels."""

from tools.container_guard import require_container

require_container()

import ast
import re


_DEBUG_SUFFIX = re.compile(rb"(?:\s|\)|\#[^\n]*(?:\n|$))*=(?!=)")


def is_debug_expression(*, source: str, formatted: ast.FormattedValue) -> bool:
    """Read the debug marker after an expression's UTF-8 source span.

    AST expressions omit enclosing parentheses. Those closing parentheses and
    whitespace may precede the debug equals sign; conversions and format specs
    follow it and do not determine whether the field uses debug syntax.
    """
    expression = formatted.value
    lines = source.encode(encoding="utf-8").splitlines(keepends=True)
    suffix = b"".join([lines[expression.end_lineno - 1][expression.end_col_offset :], *lines[expression.end_lineno :]])

    return _DEBUG_SUFFIX.match(string=suffix) is not None
