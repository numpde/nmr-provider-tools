"""Pure helpers shared by scientific code and provider-core runtime components."""

import hashlib
import math
import pathlib
import reprlib
from collections import Counter
from dataclasses import fields, is_dataclass

from .type_requirements import require_instance, require_type


# Bound scalar metadata evidence; byte fields are identified by their digests
# repo-check: allow-module-constant
_METADATA_EVIDENCE = reprlib.Repr()

_METADATA_EVIDENCE.maxstring = 256


def metadata_mismatch_message(actual_record: object, expected_record: object) -> str:
    """Describe unequal dataclasses of the same type without revalidation.

    Follow nested metadata fields; identify retained request bytes by digest.
    Callers keep the original failed comparison and own exception classification.
    """

    differing_field = next(
        (
            record_field
            for record_field in fields(actual_record)
            if record_field.compare
            if not (getattr(actual_record, record_field.name) == getattr(expected_record, record_field.name))
        ),
    )

    actual_field_value = getattr(actual_record, differing_field.name)
    expected_field_value = getattr(expected_record, differing_field.name)
    field_context = f"{differing_field.name=}: "

    if is_dataclass(actual_field_value):
        return field_context + metadata_mismatch_message(actual_field_value, expected_field_value)

    if isinstance(actual_field_value, bytes) and isinstance(expected_field_value, bytes):
        actual_field_sha256 = hashlib.sha256(actual_field_value).hexdigest()
        expected_field_sha256 = hashlib.sha256(expected_field_value).hexdigest()

        return field_context + f"{actual_field_sha256=} should match {expected_field_sha256=}"

    return (
        field_context +
        f"{_METADATA_EVIDENCE.repr(actual_field_value)=} "
        f"should match {_METADATA_EVIDENCE.repr(expected_field_value)=}"
    )


def require_identity_text(identity_text: object, identity_role: str) -> None:
    """Require nonempty printable identity text without whitespace."""
    require_type(identity_text, str)

    if not (
        identity_text and
        all(((character.isprintable()) and (not character.isspace())) for character in identity_text)
    ):
        raise ValueError(f"{identity_role=}; {reprlib.repr(identity_text)=} should be nonempty printable text without whitespace")


def duplicate_json_fields(field_pairs: list[tuple[str, object]]) -> Counter[str]:
    """Count occurrences beyond the first for each repeated JSON field."""
    field_counts = Counter(field_name for (field_name, _) in field_pairs)

    return field_counts - Counter(field_counts.keys())


def require_absolute_path(path: str | pathlib.Path) -> pathlib.Path:
    """Return an absolute NUL-free path without resolving it or accessing files."""

    require_instance(path, (str, pathlib.Path))

    path = pathlib.Path(path)

    if "\0" in str(path):
        raise ValueError(f"{path=} should not contain NUL characters")

    if not path.is_absolute():
        raise ValueError(f"{path=} should be absolute")

    return path


def require_finite_number(number: object) -> int | float:
    """Return a finite int or float, excluding booleans."""

    require_type(number, (int, float))

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
