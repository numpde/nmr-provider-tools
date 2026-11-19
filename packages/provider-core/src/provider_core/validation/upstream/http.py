"""Admit HTTP status facts and timing advice without granting retry policy.

RFC 9110 section 10.2.3 permits delay-seconds or an HTTP-date. Callers own the
wall clock and the response's authority; unadmitted advice supplies no delay.
"""

from provider_core.container_guard import require_container

require_container()

import math
import re
from collections.abc import Callable
from datetime import timezone
from email.utils import parsedate_to_datetime

from provider_core.type_requirements import require_type


def read_visible_ascii_header(
    headers: tuple[tuple[bytes, bytes], ...],
    *,
    header_name: bytes,
    max_value_bytes: int,
) -> str | None:
    """Select one nonempty visible-ASCII value under the caller's chosen cap.

    This is an optional projection, not a general HTTP header requirement.
    The caller selects the field and decides what an absent value means.
    """

    values = [value for name, value in headers if name.lower() == header_name.lower()]

    if (
        (len(values) == 1) and
        (0 < len(values[0]) <= max_value_bytes) and
        (re.fullmatch(rb"[!-~]+", values[0]) is not None)
    ):
        return values[0].decode("ascii")

    return None


def require_http_status(http_status: object) -> int:
    """Admit a numeric HTTP status independently of any service's success rule."""

    require_type(http_status, int)

    min_http_status, max_http_status = 100, 599

    if not (min_http_status <= http_status <= max_http_status):
        raise ValueError(f"{http_status=} should be between {min_http_status=} and {max_http_status=}")

    return http_status


def successful_connect_status(http_status: int) -> bool:
    """Recognize a successful CONNECT status without classifying a later TLS failure."""

    return 200 <= http_status < 300


def has_transfer_encoding(headers: tuple[tuple[bytes, bytes], ...]) -> bool:
    """Identify the transfer framing field by its case-insensitive HTTP name."""

    return any(name.lower() == b"transfer-encoding" for (name, _) in headers)


def read_retry_after(
    headers: tuple[tuple[bytes, bytes], ...],
    *,
    wall_clock: Callable[[], float],
) -> float | None:
    """Read one finite delay, sampling the supplied clock only for a parsed date."""

    retry_after_values = [value for (name, value) in headers if name.lower() == b"retry-after"]

    if len(retry_after_values) != 1:
        return None

    try:
        retry_after_text = retry_after_values[0].decode("ascii").strip()

        if re.fullmatch(r"[0-9]+", retry_after_text):
            delay_seconds = float(retry_after_text)
        else:
            retry_at = parsedate_to_datetime(retry_after_text)

            if retry_at.tzinfo is None:
                # The obsolete asctime HTTP-date form also denotes GMT
                retry_at = retry_at.replace(tzinfo=timezone.utc)

            delay_seconds = max(0.0, retry_at.timestamp() - wall_clock())
    except (ValueError, OverflowError, TypeError):
        return None

    # silent-defaults: allow-contract -- Nonfinite optional advice supplies no server-directed delay.
    return delay_seconds if math.isfinite(delay_seconds) else None
