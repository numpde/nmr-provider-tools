"""Lightweight rejection contracts shared by science and execution."""

from __future__ import annotations

import re


class CallRejected(Exception):
    """A reviewed refusal before a scientific result is established."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        if not (type(code) is (expected_type := str)):
            raise TypeError(
                f"{type(code)=} ≠ {expected_type=}"
            )

        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}\Z", code) is None:
            raise ValueError("A rejection code must match [a-z][a-z0-9_]{0,63}")

        if not (type(message) is (expected_type := str)):
            raise TypeError(
                f"{type(message)=} ≠ {expected_type=}"
            )

        if not (message):
            raise ValueError("A rejection needs a nonempty explanatory message")

        self.code = code
        self.message = message

        super().__init__(message)


class ScientificRejection(CallRejected):
    """Structurally valid input rejected for a scientific reason."""
