"""Typed, bounded payload adapters for scientific contribution calls.

The shared layer owns bytes and structural JSON admission. Contributions own the
portable dataclasses and the scientific meaning of their fields.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import json
import math
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields, is_dataclass
from enum import Enum, auto
from functools import cache
from types import NoneType, UnionType

from typing import (
    Any,
    Literal,
    Protocol,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

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


def _require_format(value: object) -> None:
    require_type(value, str)

    if not (
        value
        and all(character.isprintable() and not character.isspace() for character in value)
    ):
        raise ValueError("A payload format must be a nonempty identity without whitespace")


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


def _path_field(path: str, name: str) -> str:
    return f"{path}.{name}"


def _path_index(path: str, index: int) -> str:
    return f"{path}[{index}]"


def _union_members(annotation: object) -> tuple[object, ...] | None:
    if get_origin(annotation) in (UnionType, Union):
        return get_args(annotation)

    return None


@cache
def _resolved_hints(value_type: type[object]) -> dict[str, object]:
    return get_type_hints(value_type, include_extras=True)


def _dataclass_fields(value_type: type[object]):
    require_instance(value_type, type)

    if not is_dataclass(value_type):
        raise TypeError(f"{value_type=}; expected a dataclass type")

    parameters = value_type.__dataclass_params__

    slotted = getattr(
        parameters,
        "slots",
        "__slots__" in value_type.__dict__,
    )

    if not parameters.frozen or not slotted:
        raise TypeError(f"{value_type.__qualname__} must be a frozen, slotted dataclass")

    declared_fields = fields(value_type)

    if any(not field.init for field in declared_fields):
        non_init_field = next(field.name for field in declared_fields if not field.init)

        raise TypeError(f"{value_type.__qualname__}.{non_init_field} must participate in construction")

    annotations = _resolved_hints(value_type)
    declared_names = {field.name for field in declared_fields}

    if set(annotations) != declared_names:
        raise TypeError(f"{value_type.__qualname__} must annotate every init field and no non-field names")

    return (declared_fields, annotations)


def _validate_annotation(
    annotation: object,
    *,
    path: str,
    seen: frozenset[type[object]],
) -> None:
    if annotation in (str, bool, int, float, NoneType):
        return

    if annotation is Any:
        raise TypeError(f"{path} may not use Any")

    origin = get_origin(annotation)
    arguments = get_args(annotation)

    if origin is Literal:
        if not (arguments):
            raise TypeError(f"{path} has an empty Literal")

        return

    if origin is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            _validate_annotation(
                arguments[0],
                path=f"{path}[]",
                seen=seen,
            )

            return

        for (index, item) in enumerate(arguments):
            _validate_annotation(
                item,
                path=f"{path}[{index}]",
                seen=seen,
            )

        return

    if origin in (list, dict):
        raise TypeError(f"{path} uses a mutable container; use tuple or a nested frozen dataclass")

    members = _union_members(annotation)

    if members is not None:
        if len(members) != 2 or NoneType not in members:
            raise TypeError(f"{path} supports only T | None unions")

        _validate_annotation(
            next(member for member in members if member is not NoneType),
            path=path,
            seen=seen,
        )

        return

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return

    if isinstance(annotation, type) and is_dataclass(annotation):
        _validate_dataclass(annotation, seen=seen)

        return

    raise TypeError(f"{path} uses unsupported portable annotation {annotation!r}")


def _validate_dataclass(
    value_type: type[object],
    *,
    seen: frozenset[type[object]],
) -> None:
    if value_type in seen:
        return

    (declared_fields, hints) = _dataclass_fields(value_type)
    nested_seen = seen | {value_type}

    for field in declared_fields:
        _validate_annotation(
            hints[field.name],
            path=f"{value_type.__qualname__}.{field.name}",
            seen=nested_seen,
        )


def _decode_value(value: object, annotation: object, *, path: str) -> object:
    if annotation is str:
        if not (type(value) is (expected_type := str)):
            raise PayloadRejected(f"{path=}; {type(value)=} ≠ {expected_type=}", code=PayloadRejected.Code.INVALID_TYPE)

        return value

    if annotation is bool:
        if not (type(value) is (expected_type := bool)):
            raise PayloadRejected(f"{path=}; {type(value)=} ≠ {expected_type=}", code=PayloadRejected.Code.INVALID_TYPE)

        return value

    if annotation is int:
        if not (type(value) is (expected_type := int)):
            raise PayloadRejected(f"{path=}; {type(value)=} ≠ {expected_type=}", code=PayloadRejected.Code.INVALID_TYPE)

        return value

    if annotation is float:
        if not (type(value) in (expected_types := (int, float))):
            raise PayloadRejected(
                f"{path=}; {type(value)=} ≠ {expected_types=}",
                code=PayloadRejected.Code.INVALID_TYPE,
            )

        if not math.isfinite(value):
            raise PayloadRejected(
                f"{path} must be a finite number",
                code=PayloadRejected.Code.INVALID_TYPE,
            )

        return float(value)

    if annotation is NoneType:
        if not (type(value) is (expected_type := NoneType)):
            raise PayloadRejected(f"{path=}; {type(value)=} ≠ {expected_type=}", code=PayloadRejected.Code.INVALID_TYPE)

        return None

    origin = get_origin(annotation)
    arguments = get_args(annotation)

    if origin is Literal:
        if not (value in arguments):
            raise PayloadRejected(
                f"{path} is not one of the declared literal values",
                code=PayloadRejected.Code.INVALID_VALUE,
            )

        expected_types = [type(item) for item in arguments]

        if not any(type(value) is expected_type for expected_type in expected_types):
            raise PayloadRejected(
                f"{path=}; {type(value)=} ≠ {expected_types=}",
                code=PayloadRejected.Code.INVALID_VALUE,
            )

        return value

    if origin is tuple:
        if not (type(value) is (expected_type := list)):
            raise PayloadRejected(f"{path=}; {type(value)=} ≠ {expected_type=}", code=PayloadRejected.Code.INVALID_TYPE)

        if len(arguments) == 2 and arguments[1] is Ellipsis:
            # repo-check: allow-tuple-generator -- Decode the provider's variadic tuple annotation into the immutable type its scientific callable requires.
            return tuple(
                _decode_value(
                    item,
                    arguments[0],
                    path=_path_index(path, index),
                )
                for (index, item) in enumerate(value)
            )

        if len(value) != len(arguments):
            raise PayloadRejected(
                f"{path} must contain exactly {len(arguments)} items",
                code=PayloadRejected.Code.INVALID_LENGTH,
            )

        # repo-check: allow-tuple-generator -- A fixed tuple annotation requires an immutable positional result with the declared element types.
        return tuple(
            _decode_value(
                item,
                expected,
                path=_path_index(path, index),
            )
            for (index, (item, expected)) in enumerate(
                zip(value, arguments, strict=True)
            )
        )

    members = _union_members(annotation)

    if members is not None:
        if value is None:
            return None

        member = next(item for item in members if item is not NoneType)

        return _decode_value(value, member, path=path)

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        try:
            return annotation(value)
        except (TypeError, ValueError) as error:
            raise PayloadRejected(
                f"{path} is not a declared enum value",
                code=PayloadRejected.Code.INVALID_VALUE,
            ) from error

    if isinstance(annotation, type) and is_dataclass(annotation):
        return _decode_dataclass(value, annotation, path=path)

    raise AssertionError(f"Unsupported annotation reached decoding: {annotation!r}")


def _decode_dataclass(value: object, value_type: type[T], *, path: str) -> T:
    if not (type(value) is (expected_type := dict)):
        raise PayloadRejected(f"{path=}; {type(value)=} ≠ {expected_type=}", code=PayloadRejected.Code.INVALID_TYPE)

    (declared_fields, annotations) = _dataclass_fields(value_type)
    fields_by_name = {field.name: field for field in declared_fields}
    unexpected_fields = set(value) - set(fields_by_name)

    if unexpected_fields:
        raise PayloadRejected(
            f"{path} contains unexpected fields: {sorted(unexpected_fields)!r}",
            code=PayloadRejected.Code.UNEXPECTED_FIELD,
        )

    missing_fields = {
        name
        for (name, field) in fields_by_name.items()
        if
        (name not in value) and
        (field.default is MISSING) and
        (field.default_factory is MISSING)
    }

    if missing_fields:
        raise PayloadRejected(
            f"{path} is missing required fields: {sorted(missing_fields)!r}",
            code=PayloadRejected.Code.MISSING_FIELD,
        )

    decoded_arguments = {
        name: _decode_value(
            item,
            annotations[name],
            path=_path_field(path, name),
        )
        for (name, item) in value.items()
    }

    try:
        return value_type(**decoded_arguments)
    except CallRejected:
        raise
    except (TypeError, ValueError) as error:
        raise PayloadRejected(
            f"{path} could not construct {value_type.__qualname__}",
            code=PayloadRejected.Code.INVALID_VALUE,
        ) from error


def _encode_value(value: object, annotation: object, *, path: str) -> object:
    if annotation is str:
        if not (type(value) is (expected_type := str)):
            raise TypeError(f"{path=}; {type(value)=} ≠ {expected_type=}")

        return value

    if annotation is bool:
        if not (type(value) is (expected_type := bool)):
            raise TypeError(f"{path=}; {type(value)=} ≠ {expected_type=}")

        return value

    if annotation is int:
        if not (type(value) is (expected_type := int)):
            raise TypeError(f"{path=}; {type(value)=} ≠ {expected_type=}")

        return value

    if annotation is float:
        if not (type(value) in (expected_types := (int, float))):
            raise TypeError(f"{path=}; {type(value)=} ≠ {expected_types=}")

        if not math.isfinite(value):
            raise TypeError(f"{path} must be a finite float-compatible number")

        return float(value)

    if annotation is NoneType:
        if not (type(value) is (expected_type := NoneType)):
            raise TypeError(f"{path=}; {type(value)=} ≠ {expected_type=}")

        return None

    origin = get_origin(annotation)
    arguments = get_args(annotation)

    if origin is Literal:
        if not (value in arguments):
            raise TypeError(f"{path} is not one of the declared literal values")

        expected_types = [type(item) for item in arguments]

        if not any(type(value) is expected_type for expected_type in expected_types):
            raise TypeError(f"{path=}; {type(value)=} ≠ {expected_types=}")

        return value

    if origin is tuple:
        if not (type(value) is (expected_type := tuple)):
            raise TypeError(f"{path=}; {type(value)=} ≠ {expected_type=}")

        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return [
                _encode_value(
                    item,
                    arguments[0],
                    path=_path_index(path, index),
                )
                for (index, item) in enumerate(value)
            ]

        if len(value) != len(arguments):
            raise TypeError(f"{path} must contain exactly {len(arguments)} items")

        return [
            _encode_value(
                item,
                expected,
                path=_path_index(path, index),
            )
            for (index, (item, expected)) in enumerate(
                zip(value, arguments, strict=True)
            )
        ]

    members = _union_members(annotation)

    if members is not None:
        if value is None:
            return None

        member = next(item for item in members if item is not NoneType)

        return _encode_value(value, member, path=path)

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if not (type(value) is (expected_type := annotation)):
            raise TypeError(f"{path=}; {type(value)=} ≠ {expected_type=}")

        return value.value

    if isinstance(annotation, type) and is_dataclass(annotation):
        return _encode_dataclass(value, annotation, path=path)

    raise AssertionError(f"Unsupported annotation reached encoding: {annotation!r}")


def _encode_dataclass(
    value: object,
    value_type: type[object],
    *,
    path: str,
) -> dict[str, object]:
    if not (type(value) is (expected_type := value_type)):
        raise TypeError(f"{path=}; {type(value)=} ≠ {expected_type=}")

    (declared_fields, hints) = _dataclass_fields(value_type)

    return {
        field.name: _encode_value(
            getattr(value, field.name),
            hints[field.name],
            path=_path_field(path, field.name),
        )
        for field in declared_fields
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class JsonDataclassCodec[T]:
    """Strict JSON serialization for one provider-owned portable dataclass."""

    format: str
    value_type: type[T]

    def __post_init__(self) -> None:
        _require_format(self.format)
        _validate_dataclass(self.value_type, seen=frozenset())

    def json_schema(self) -> dict:
        """Describe this codec's input structure without replacing runtime admission.

        Requires provider-core[api]; native scientific execution needs no schema
        library. The document is independent of other exports and keeps local
        definitions at its root so consumers can use it directly as parameters.
        """

        # Load optional schema dependencies only when export is requested.
        from provider_core._json_schema import dataclass_json_schema

        return dataclass_json_schema(value_type=self.value_type)

    def decode(self, payload: bytes) -> T:
        return _decode_dataclass(
            _read_json(payload),
            self.value_type,
            path="$",
        )

    def encode(self, value: T) -> bytes:
        document = _encode_dataclass(
            value,
            self.value_type,
            path="$",
        )

        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")


@dataclass(frozen=True, slots=True, kw_only=True)
class DataclassStateBinding[T]:
    """Bind exact named live values into a provider-owned dataclass."""

    value_type: type[T]

    def __post_init__(self) -> None:
        (declared_fields, hints) = _dataclass_fields(self.value_type)

        for field in declared_fields:
            annotation = hints[field.name]

            if get_origin(annotation) is not None or annotation in (Any, object):
                raise TypeError(
                    f"{self.value_type.__qualname__}.{field.name} "
                    f"must name a concrete live-state class"
                )

    @property
    def names(self) -> tuple[str, ...]:
        # repo-check: allow-tuple-generator -- StateBinding.names exposes immutable field order for matching declared live-state inputs.
        return tuple(field.name for field in fields(self.value_type))

    def bind(self, values: Mapping[str, object]) -> T:
        require_instance(values, Mapping)

        if set(values) != set(self.names):
            raise ValueError("Live scientific state names differ from the declared state struct")

        annotations = _resolved_hints(self.value_type)

        for (name, state_value) in values.items():
            expected_type = annotations[name]

            if not (type(state_value) is expected_type):
                raise TypeError(f"{name=}; {type(state_value)=} ≠ {expected_type=}")

        return self.value_type(**values)
