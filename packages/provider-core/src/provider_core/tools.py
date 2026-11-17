"""Small primitives shared by provider-core's independent runtime components."""

from provider_core.container_guard import require_container

require_container()

import math
from pathlib import Path


def require_absolute_path(path: str | Path) -> Path:
    """Return an absolute NUL-free path without resolving it or accessing files."""
    if not isinstance(path, (expected_types := (str, Path))):
        raise TypeError(f"{type(path)=} ∉ {expected_types=}")

    path = Path(path)

    if "\0" in str(path):
        raise ValueError(f"{path=} should not contain NUL characters")

    if not path.is_absolute():
        raise ValueError(f"{path=} should be absolute")

    return path


def require_positive_number(number):
    """Return a finite positive int or float, excluding booleans."""
    if not (type(number) in (expected_types := (int, float))):
        raise TypeError(f"{type(number)=} ∉ {expected_types=}")

    if not math.isfinite(number):
        raise ValueError(f"{number=} should be finite")

    if not (0 < number):
        raise ValueError(f"{number=} should be positive")

    return number


def require_nonnegative_number(number):
    """Return a finite nonnegative int or float, excluding booleans."""
    if not (type(number) in (expected_types := (int, float))):
        raise TypeError(f"{type(number)=} ∉ {expected_types=}")

    if not math.isfinite(number):
        raise ValueError(f"{number=} should be finite")

    if not (0 <= number):
        raise ValueError(f"{number=} should be nonnegative")

    return number
