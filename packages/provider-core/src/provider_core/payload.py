"""Typed, bounded payload adapters for scientific contribution calls.

The shared layer owns bytes and structural JSON admission. Contributions own the
portable dataclasses and the scientific meaning of their fields.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import json
import reprlib
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import auto
from functools import cache

from typing import (
    Any,
    Protocol,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from provider_core._json_schema import _adapter, dataclass_json_schema
from provider_core.contribution_errors import CallRejected, ErrorCode
from provider_core.tools import duplicate_json_fields
from provider_core.type_requirements import require_instance, require_type


class PayloadRejected(CallRejected):
    """The payload does not match its declared portable representation."""

    class Code(ErrorCode):
        INVALID_JSON = auto()
        DUPLICATE_FIELD = auto()
        INVALID_TYPE = auto()
        INVALID_VALUE = auto()
        INVALID_LENGTH = auto()
        UNEXPECTED_FIELD = auto()
        MISSING_FIELD = auto()

    def __init__(self, message: str, *, code: PayloadRejected.Code) -> None:
        require_instance(code, self.Code)

        super().__init__(code=code, message=message)


@runtime_checkable
class PayloadCodec[T](Protocol):
    """Convert one declared payload format to and from a provider-owned type."""

    @property
    def format(self) -> str: ...

    @property
    def value_type(self) -> type[T]: ...

    def decode(self, payload: bytes) -> T: ...

    def encode(self, value: T) -> bytes: ...


@runtime_checkable
class StateBinding[T](Protocol):
    """Turn host-owned named live values into one provider-owned state struct."""

    @property
    def names(self) -> tuple[str, ...]: ...

    @property
    def value_type(self) -> type[T]: ...

    def bind(self, values: Mapping[str, object]) -> T: ...


def _require_format(payload_format: object) -> None:
    require_type(payload_format, str)

    if not (
        (payload_format) and
        all(((character.isprintable()) and (not character.isspace())) for character in payload_format)
    ):
        raise ValueError(f"{reprlib.repr(payload_format)=} should be nonempty printable text without whitespace")


def _reject_constant(nonfinite_token: str) -> None:
    raise PayloadRejected(f"JSON has {nonfinite_token=}", code=PayloadRejected.Code.INVALID_JSON)


def _unique_fields(field_pairs: list[tuple[str, object]]) -> dict[str, object]:
    if duplicate_fields := duplicate_json_fields(field_pairs):
        raise PayloadRejected(f"JSON has {duplicate_fields=}", code=PayloadRejected.Code.DUPLICATE_FIELD)

    return dict(field_pairs)


def _read_json(payload: bytes) -> object:
    require_type(payload, bytes)

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as error:
        invalid_utf8_byte_offset = error.start

        raise PayloadRejected(
            f"Payload has {invalid_utf8_byte_offset=}",
            code=PayloadRejected.Code.INVALID_JSON,
        ) from error
    except json.JSONDecodeError as error:
        line_number = error.lineno
        column_number = error.colno

        raise PayloadRejected(
            f"Payload JSON is invalid at {line_number=}, {column_number=}",
            code=PayloadRejected.Code.INVALID_JSON,
        ) from error


@cache
def _resolved_hints(value_type: type[object]) -> dict[str, object]:
    return get_type_hints(value_type, include_extras=True)


def _dataclass_fields(value_type: type[object]):
    require_instance(value_type, type)

    if not is_dataclass(value_type):
        raise TypeError(f"{value_type=}; expected a dataclass type")

    declared_fields = fields(value_type)

    if any((not field.init) for field in declared_fields):
        non_init_field = next(field.name for field in declared_fields if not field.init)

        raise TypeError(f"{value_type.__qualname__ = }.{non_init_field = } must participate in construction")

    annotations = _resolved_hints(value_type)
    declared_names = {field.name for field in declared_fields}

    if set(annotations) != declared_names:
        raise TypeError(f"{value_type.__qualname__ = } must annotate every init field and no non-field names")

    return (declared_fields, annotations)


@dataclass(frozen=True, slots=True, kw_only=True)
class JsonDataclassCodec[T]:
    """Use Pydantic's type semantics within the SDK's JSON transport contract.

    Incoming JSON rejects duplicate keys and nonfinite numbers, and Pydantic
    validates fields in strict mode with unknown record fields forbidden.
    Serialization uses Pydantic's documented conversions and treats serializer
    warnings as errors. Scientific validity remains the contribution's concern.
    """

    format: str
    value_type: type[T]

    def __post_init__(self) -> None:
        _require_format(self.format)
        require_instance(self.value_type, type)

        if not is_dataclass(self.value_type):
            raise TypeError(f"{self.value_type=} should be a dataclass type")

        _adapter(value_type=self.value_type)

    def json_schema(self) -> dict:
        return dataclass_json_schema(value_type=self.value_type)

    def decode(self, payload: bytes) -> T:
        # Retain lexical JSON checks that are independent of the scientific type
        document = _read_json(payload)

        try:
            encoded = json.dumps(document, allow_nan=False)
            return _adapter(value_type=self.value_type).validate_json(encoded, strict=True, extra="forbid")
        except (ValidationError, ValueError) as error:
            raise PayloadRejected(str(error), code=PayloadRejected.Code.INVALID_VALUE) from error

    def encode(self, value: T) -> bytes:
        require_type(value, self.value_type)

        try:
            document = _adapter(value_type=self.value_type).dump_python(value, mode="json", warnings="error")
        except PydanticSerializationError as error:
            raise TypeError(str(error)) from error

        return json.dumps(document, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True, slots=True, kw_only=True)
class DataclassStateBinding[T]:
    """Bind exact named live values into a provider-owned dataclass."""

    value_type: type[T]

    def __post_init__(self) -> None:
        (declared_fields, hints) = _dataclass_fields(self.value_type)

        for field in declared_fields:
            annotation = hints[field.name]

            if (get_origin(annotation) is not None) or (annotation in (Any, object)):
                raise TypeError(
                    f"{self.value_type.__qualname__ = }.{field.name = } "
                    f"must name a concrete live-state class"
                )

    @property
    def names(self) -> tuple[str, ...]:
        # repo-check: allow-tuple-generator -- StateBinding.names exposes immutable field order for matching declared live-state inputs.
        return tuple(field.name for field in fields(self.value_type))

    def bind(self, values: Mapping[str, object]) -> T:
        require_instance(values, Mapping)

        received_state_names = set(values)
        expected_state_names = set(self.names)

        if not (received_state_names == expected_state_names):
            raise ValueError(f"{reprlib.repr(received_state_names)=} should match {reprlib.repr(expected_state_names)=}")

        annotations = _resolved_hints(self.value_type)

        for (name, state_value) in values.items():
            expected_type = annotations[name]

            require_type(
                state_value,
                expected_type,
                error_factory=lambda diagnostic: TypeError(f"{name=}; " + diagnostic),
            )

        return self.value_type(**values)
