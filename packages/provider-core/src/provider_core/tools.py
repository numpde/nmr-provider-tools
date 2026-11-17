"""Small primitives shared by provider-core's independent runtime components."""

from provider_core.container_guard import require_container

require_container()

import math


def positive_seconds(seconds):
    """Require finite positive numeric seconds and return the admitted value."""
    if not (type(seconds) in (expected_type := (int, float))):
        raise TypeError(f"{type(seconds)=} ≠ {expected_type=}")

    if not (math.isfinite(seconds) and 0 < seconds):
        raise ValueError(f"{seconds=}; expected finite positive seconds")

    return seconds
