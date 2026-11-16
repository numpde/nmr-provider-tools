"""Scalar finiteness checks used by the dummy provider example."""

import math


def isfinite(numeric_scalar: int | float, /) -> bool:
    """Return whether one scalar is finite; NaN and either infinity are false."""

    return math.isfinite(numeric_scalar)
