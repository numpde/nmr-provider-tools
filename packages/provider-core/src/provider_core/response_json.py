"""Decode bounded API JSON without exposing private payload text in errors.

Route-specific modules own schema and identity admission. This shared decoder
rejects ambiguous JSON before either Upload or Attempt facts are interpreted.
"""

from provider_core.container_guard import require_container

require_container()

import json
import re
from datetime import datetime


class ResponseRejected(ValueError):
    """Safe wire rejection; the API owner retains the original response privately."""


def _unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ResponseRejected("Response contains a duplicate JSON field")
        result[name] = value
    return result


def _reject_constant(_value):
    raise ResponseRejected("Response contains a nonfinite JSON number")


def read_document(raw, limit):
    if type(raw) is not bytes or len(raw) > limit:
        raise ResponseRejected(f"Response must be immutable bytes within the {limit}-byte route limit")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
    except ResponseRejected:
        raise
    except UnicodeDecodeError as cause:
        raise ResponseRejected(f"Response has invalid UTF-8 at byte {cause.start}") from None
    except json.JSONDecodeError as cause:
        raise ResponseRejected(f"Response has invalid JSON at line {cause.lineno}, column {cause.colno}") from None
    except RecursionError:
        raise ResponseRejected("Response exceeds the JSON nesting limit") from None
    except ValueError:
        raise ResponseRejected("Response exceeds the JSON number limit") from None


def read_timestamp(value):
    """Admit UTC rendering shared by the pinned provider Job and Attempt schemas."""
    if (
        type(value) is not str
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.(?!000000)[0-9]{6})?Z", value) is None
    ):
        raise ResponseRejected("Response timestamp must be canonical UTC seconds or six nonzero fractional digits")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ResponseRejected("Response timestamp is not a valid calendar date and time") from None
