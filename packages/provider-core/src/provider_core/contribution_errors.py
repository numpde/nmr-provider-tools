"""Lightweight rejection contracts shared by science and execution."""

from __future__ import annotations

import re
from enum import Enum, StrEnum

from provider_core.type_requirements import require_instance, require_type


class ErrorCode(Enum):
    """Give auto() members their lowercase names as values."""

    @staticmethod
    def _generate_next_value_(name, _start, _count, _last_values):
        return name.lower()


class CallRejected(Exception):
    """A reviewed refusal before a scientific result is established."""

    code: str | ErrorCode
    message: str

    def __init__(self, code: str | ErrorCode, message: str) -> None:
        if type(code) is not str:
            require_instance(code, (StrEnum, ErrorCode))

        code_text = code.value if isinstance(code, ErrorCode) else code

        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}\Z", code_text) is None:
            raise ValueError("A rejection code must match [a-z][a-z0-9_]{0,63}")

        require_type(message, str)

        if not (message):
            raise ValueError("A rejection needs a nonempty explanatory message")

        self.code = code
        self.message = message

        super().__init__(message)


class ScientificRejection(CallRejected):
    """Structurally valid input rejected for a scientific reason."""
