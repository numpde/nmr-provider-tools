"""Pure helpers shared by scientific code and provider-core runtime components."""

import math
import pathlib
from collections import Counter


def duplicate_json_fields(field_pairs: list[tuple[str, object]]) -> Counter[str]:
    """Count occurrences beyond the first for each repeated JSON field."""
    field_counts = Counter(field_name for (field_name, _) in field_pairs)

    return field_counts - Counter(field_counts.keys())


def require_absolute_path(path: str | pathlib.Path) -> pathlib.Path:
    """Return an absolute NUL-free path without resolving it or accessing files."""
    if not isinstance(path, (expected_types := (str, pathlib.Path))):
        raise TypeError(f"{type(path)=} ∉ {expected_types=}")

    path = pathlib.Path(path)

    if "\0" in str(path):
        raise ValueError(f"{path=} should not contain NUL characters")

    if not path.is_absolute():
        raise ValueError(f"{path=} should be absolute")

    return path


def require_finite_number(number: object) -> int | float:
    """Return a finite int or float, excluding booleans."""
    if not (type(number) in (expected_types := (int, float))):
        raise TypeError(f"{type(number)=} ∉ {expected_types=}")

    if not math.isfinite(number):
        raise ValueError(f"{number=} should be finite")

    return number


def require_positive_number(number):
    """Return a finite positive int or float, excluding booleans."""
    require_finite_number(number)

    if not (0 < number):
        raise ValueError(f"{number=} should be positive")

    return number


def require_nonnegative_number(number):
    """Return a finite nonnegative int or float, excluding booleans."""
    require_finite_number(number)

    if not (0 <= number):
        raise ValueError(f"{number=} should be nonnegative")

    return number
