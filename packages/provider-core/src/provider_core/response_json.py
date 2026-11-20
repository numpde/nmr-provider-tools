"""Decode bounded JSON before schema-specific admission.

Callers own schema, identity and timestamp admission. This shared decoder rejects
ambiguous JSON before API, Chat, credential or local IPC facts are interpreted.
"""

from provider_core.container_guard import require_container

require_container()

import json

from provider_core.tools import duplicate_json_fields
from provider_core.type_requirements import require_type


class ResponseRejected(ValueError):
    """Wire admission failure with the original decoder or validator as its cause."""


def _unique_fields(field_pairs):
    if not ((duplicate_fields := duplicate_json_fields(field_pairs)) == {}):
        raise ResponseRejected(f"{duplicate_fields=} should be empty")

    return dict(field_pairs)


def _reject_constant(nonfinite_token):
    raise ResponseRejected(f"{nonfinite_token=} should be a finite JSON number")


def read_document(response_bytes, maximum_bytes):
    require_type(response_bytes, bytes, error_factory=ResponseRejected)

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
