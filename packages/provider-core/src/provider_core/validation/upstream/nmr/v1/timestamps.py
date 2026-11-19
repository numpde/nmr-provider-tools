"""Admit the NMR API's canonical UTC rendering before returning a datetime.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/json_datetime.py. This is a service profile, not general JSON syntax.
"""

from provider_core.container_guard import require_container

require_container()

import re
import reprlib
from datetime import datetime

from provider_core.response_json import ResponseRejected


def read_timestamp(timestamp):
    """Admit UTC seconds or a nonzero six-digit fraction and a real calendar date."""

    if not (type(timestamp) is (expected_type := str)):
        raise ResponseRejected(f"{type(timestamp)=} ≠ {expected_type=}")

    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.(?!000000)[0-9]{6})?Z", timestamp) is None:
        raise ResponseRejected(f"{reprlib.repr(timestamp)=} should use canonical UTC seconds or nonzero microseconds")

    try:
        return datetime.fromisoformat(timestamp)
    except ValueError as cause:
        raise ResponseRejected(f"Cannot read {timestamp=}") from cause
