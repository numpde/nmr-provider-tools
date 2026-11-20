"""Bind documented guard exceptions to one adjacent statement."""

from tools.container_guard import require_container

require_container()

import io
import tokenize


def documented_guard_indents(source: str, *, prefix: str) -> dict[int, int]:
    """Map next-line guards to the indentation of real, reasoned comments."""

    return {
        (token.start[0] + 1): token.start[1]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if
        (token.type == tokenize.COMMENT) and
        (not token.line[:token.start[1]].strip()) and
        token.string.startswith(prefix) and
        token.string.removeprefix(prefix).strip()
    }
