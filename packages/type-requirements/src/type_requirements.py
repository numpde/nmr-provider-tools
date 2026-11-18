"""Assert exact types, instances, and subclasses with call-site expressions."""

import ast
import inspect
import linecache


__all__ = ["require_type", "require_instance", "require_subclass"]


def _allowed_types(T: type | tuple[type, ...]) -> tuple[type, ...]:
    types = T if isinstance(T, tuple) else (T,)

    if not types or not all(isinstance(item, type) for item in types):
        raise TypeError("T must be a type or a non-empty tuple of types")

    return types


def _argument_source(caller) -> str | None:
    """Return the caller's expr source when the original file is available."""
    position = inspect.getframeinfo(caller).positions

    if position is None or None in (
        position.lineno,
        position.col_offset,
        position.end_lineno,
        position.end_col_offset,
    ):
        return None

    filename = caller.f_code.co_filename
    source = "".join(linecache.getlines(filename, caller.f_globals))

    if not source:
        return None

    location = (
        position.lineno,
        position.col_offset,
        position.end_lineno,
        position.end_col_offset,
    )

    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue

        if (
            node.lineno,
            node.col_offset,
            node.end_lineno,
            node.end_col_offset,
        ) != location:
            continue

        if node.args:
            if isinstance(node.args[0], ast.Starred):
                return None

            return ast.get_source_segment(source, node.args[0])

        for keyword in node.keywords:
            if keyword.arg == "expr":
                return ast.get_source_segment(source, keyword.value)

        return None

    return None


def _raise_mismatch(expr: object, T: type | tuple[type, ...], relation: str) -> None:
    expected = (
        f"one of ({', '.join(item.__name__ for item in T)})"
        if isinstance(T, tuple)
        else T.__name__
    )

    if relation == "instance":
        expected = f"instance of {expected}"
    elif relation == "subclass":
        expected = f"subclass of {expected}"

    actual = type(expr).__name__

    if relation == "subclass":
        actual = expr.__name__ if isinstance(expr, type) else f"{actual} instance"

    frame = inspect.currentframe()
    caller = frame.f_back.f_back if frame and frame.f_back else None

    try:
        try:
            source_expr = _argument_source(caller) if caller else None
        except (OSError, SyntaxError, UnicodeError):
            source_expr = None
    finally:
        del frame
        del caller

    if source_expr is not None:
        subject = source_expr
    else:
        try:
            subject = repr(expr)
        except Exception:
            subject = f"<{actual}>"

    raise TypeError(f"{subject}: expected {expected}, got {actual}")


def require_type(expr: object, T: type | tuple[type, ...]) -> None:
    """Require exact runtime type identity (not subclasses)."""
    allowed_types = _allowed_types(T)

    if any(type(expr) is allowed for allowed in allowed_types):
        return

    _raise_mismatch(expr, T, "type")


def require_instance(expr: object, T: type | tuple[type, ...]) -> None:
    """Require isinstance semantics, including subclasses and virtual ABCs."""
    allowed_types = _allowed_types(T)

    if isinstance(expr, allowed_types):
        return

    _raise_mismatch(expr, T, "instance")


def require_subclass(expr: object, T: type | tuple[type, ...]) -> None:
    """Require a class that satisfies issubclass semantics."""
    allowed_types = _allowed_types(T)

    if isinstance(expr, type) and issubclass(expr, allowed_types):
        return

    _raise_mismatch(expr, T, "subclass")
