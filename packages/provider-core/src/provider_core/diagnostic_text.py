"""Scrub recognizable credentials at operator text and logging boundaries.

Messages otherwise remain intact. This does not identify every private value;
callers must not deliberately dump request headers, bodies or frame locals.
"""

from provider_core.container_guard import require_container

require_container()

import logging
import re


# Redaction recognizes only this reviewed credential grammar.
# repo-check: allow-module-constant
_CREDENTIAL = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[^\s\"'<>;,]+|\bsk-[A-Za-z0-9_.-]+")

# Match entire quoted secrets, including escaped quotes, so their suffixes
# cannot escape redaction. Preserve the existing diagnostic field grammar.
# repo-check: allow-module-constant
_SECRET_FIELD = re.compile(
    r"(?i)(\b(?:api[_-]?key|password|token|secret|authorization)\b[\"']?\s*[:=]\s*)"
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&<>]+)"""
)


def redact_credential_fields(text):
    """Redact recognizable credentials; other private request data may remain."""

    return _SECRET_FIELD.sub(r"\1[redacted]", _CREDENTIAL.sub("[redacted]", text))


def without_echoes(text: str, echoes: tuple[str, ...]) -> str:
    """Remove complete overlapping spellings before normalizing diagnostic text.

    SECS7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    src/secs_inference/provider/chat.py::_diagnostic_text merges original
    matches so replacing one spelling cannot expose a larger overlapping echo.
    """

    spans = []

    for echo in echoes:
        start = text.find(echo)

        while start >= 0:
            spans.append((start, start + len(echo)))
            start = text.find(echo, start + 1)

    (parts, end) = ([], 0)

    for (start, stop) in sorted(spans):
        if start >= end:
            parts.extend((text[end:start], "[redacted]"))

        end = max(end, stop)

    parts.append(text[end:])

    # silent-defaults: allow-contract -- Normalize nonprinting characters while preserving the readable redacted structure.
    return " ".join("".join((char if char.isprintable() else " ") for char in "".join(parts)).split())


class CredentialRedactingFormatter(logging.Formatter):
    """Scrub the final output, including tracebacks, without changing errors."""

    def format(self, record):
        return redact_credential_fields(super().format(record))
