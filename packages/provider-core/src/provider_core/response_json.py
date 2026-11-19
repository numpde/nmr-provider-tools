"""Decode bounded API JSON before route-specific admission.

Route-specific modules own schema and identity admission. This shared decoder
rejects ambiguous JSON before either Upload or Attempt facts are interpreted.
"""

from provider_core.container_guard import require_container

require_container()

import json
import re
from datetime import datetime

from provider_core.tools import duplicate_json_fields


class ResponseRejected(ValueError):
    """Wire admission failure with the original decoder or validator as its cause."""


def _unique_fields(field_pairs):
    if duplicate_fields := duplicate_json_fields(field_pairs):
        raise ResponseRejected(f"{duplicate_fields=} should be empty")

    return dict(field_pairs)


def _reject_constant(nonfinite_token):
    raise ResponseRejected(f"{nonfinite_token=} should be a finite JSON number")


def read_document(response_bytes, maximum_bytes):
    if not (type(response_bytes) is (expected_type := bytes)):
        raise ResponseRejected(f"{type(response_bytes)=} ≠ {expected_type=}")

    response_byte_length = len(response_bytes)

    if not (response_byte_length <= maximum_bytes):
        raise ResponseRejected(f"{response_byte_length=} should be at most {maximum_bytes=}")

    try:
        return json.loads(response_bytes.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
    except ResponseRejected:
        raise
    except UnicodeDecodeError as cause:
        raise ResponseRejected("response_bytes should be UTF-8") from cause
    except json.JSONDecodeError as cause:
        raise ResponseRejected("response_bytes should encode JSON") from cause
    except RecursionError as cause:
        raise ResponseRejected("JSON nesting should fit the decoder's recursion limit") from cause
    except ValueError as cause:
        raise ResponseRejected("Cannot decode response_bytes") from cause


def read_timestamp(timestamp):
    """Admit UTC rendering shared by the pinned provider Job and Attempt schemas."""

    if not (type(timestamp) is (expected_type := str)):
        raise ResponseRejected(f"{type(timestamp)=} ≠ {expected_type=}")

    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.(?!000000)[0-9]{6})?Z", timestamp) is None:
        raise ResponseRejected(f"{timestamp=} should use canonical UTC seconds or nonzero microseconds")

    try:
        return datetime.fromisoformat(timestamp)
    except ValueError as cause:
        raise ResponseRejected(f"Cannot read {timestamp=}") from cause
