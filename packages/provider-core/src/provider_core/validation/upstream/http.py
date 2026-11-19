"""Read optional HTTP timing advice without granting retry or pacing policy.

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
