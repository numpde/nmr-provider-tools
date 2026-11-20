"""Match complete expression spans with their source grouping delimiters."""

from tools.container_guard import require_container

require_container()

import ast
import io
import tokenize
from collections.abc import Iterable


_OPENERS = {"(": ")", "[": "]", "{": "}"}


def delimiter_pairs(*, source: str) -> dict[tuple[int, int], tokenize.TokenInfo]:
    """Map each opening delimiter's UTF-8 position to its matching closer."""

    stack = []
    pairs = {}
    lines = source.splitlines()

    for token in tokenize.generate_tokens(readline=io.StringIO(initial_value=source).readline):
        if token.type != tokenize.OP:
            continue

        if token.string in _OPENERS:
            stack.append(token)
        elif (token.string in _OPENERS.values()) and stack and (_OPENERS[stack[-1].string] == token.string):
            opening = stack.pop()
            opening_byte_column = len(lines[opening.start[0] - 1][:opening.start[1]].encode(encoding="utf-8"))
            pairs[(opening.start[0], opening_byte_column)] = token

    return pairs


def character_column(*, line: str, byte_column: int) -> int:
    """Convert an AST UTF-8 byte offset to a tokenizer character column."""

    return len(line.encode(encoding="utf-8")[:byte_column].decode(encoding="utf-8"))


def needs_operator_grouping(operand: ast.expr) -> bool:
    """Distinguish operator expressions from atoms and signed numeric literals."""

    if (
        isinstance(operand, ast.UnaryOp) and
        isinstance(operand.op, (ast.UAdd, ast.USub)) and
        isinstance(operand.operand, ast.Constant) and
        (type(operand.operand.value) in (int, float, complex))
    ):
        return False

    return isinstance(operand, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.IfExp, ast.NamedExpr, ast.Compare))


def ungrouped_expression_findings(*, source: str, expressions: Iterable[ast.expr]) -> list[tuple[int, int, str]]:
    """Locate expressions without parentheses enclosing their complete token span."""

    lines = source.splitlines()

    tokens = [
        token
        for token in tokenize.generate_tokens(readline=io.StringIO(source).readline)
        if token.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    ]

    starts = {token.start: index for (index, token) in enumerate(tokens)}
    ends = {token.end: index for (index, token) in enumerate(tokens)}
    pairs = delimiter_pairs(source=source)
    findings = []

    for expression in expressions:
        start = (expression.lineno, character_column(line=lines[expression.lineno - 1], byte_column=expression.col_offset))
        end = (expression.end_lineno, character_column(line=lines[expression.end_lineno - 1], byte_column=expression.end_col_offset))
        first = starts[start]
        last = ends[end]
        before = tokens[first - 1] if first else None
        after = tokens[last + 1] if (last + 1) < len(tokens) else None

        if (before is not None) and (after is not None) and (before.string == "(") and (after.string == ")"):
            opening = (
                before.start[0],
                len(lines[before.start[0] - 1][:before.start[1]].encode("utf-8")),
            )

            if pairs.get(opening) == after:
                continue

        findings.append((start[0], start[1] + 1, ast.get_source_segment(source, expression)))

    return sorted(findings)
