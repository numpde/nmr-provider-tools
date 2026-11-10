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
from enum import Enum
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

from provider_core.contribution_errors import CallRejected


class PayloadRejected(CallRejected):
    """The payload does not match its declared portable representation."""


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
    if not (
        type(value) is str
        and value
        and all(character.isprintable() and not character.isspace() for character in value)
    ):
        raise ValueError("A payload format must be a nonempty identity without whitespace")


def _reject_constant(value: str) -> None:
    raise PayloadRejected(
        "invalid_json",
        f"JSON constant {value!r} is not permitted",
    )


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for (name, value) in pairs:
        if name in result:
            raise PayloadRejected(
                "duplicate_field",
                f"JSON field {name!r} appears more than once",
            )
        result[name] = value
    return result


def _read_json(payload: bytes) -> object:
    if type(payload) is not bytes:
        raise TypeError("Payload codecs accept bytes")
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except PayloadRejected:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadRejected(
            "invalid_json",
            "Payload must be one complete UTF-8 JSON value",
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
    if not (isinstance(value_type, type) and is_dataclass(value_type)):
        raise TypeError("Portable values must be dataclass types")
    parameters = value_type.__dataclass_params__
    slotted = getattr(
        parameters,
        "slots",
        "__slots__" in value_type.__dict__,
    )
    if not parameters.frozen or not slotted:
        raise TypeError(
            f"{value_type.__qualname__} must be a frozen, slotted dataclass"
        )
    result = fields(value_type)
    if any(not field.init for field in result):
        name = next(field.name for field in result if not field.init)
        raise TypeError(
            f"{value_type.__qualname__}.{name} must participate in construction"
        )
    hints = _resolved_hints(value_type)
    declared = {field.name for field in result}
    if set(hints) != declared:
        raise TypeError(
            f"{value_type.__qualname__} must annotate every init field and no non-field names"
        )
    return result, hints


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
        if not arguments:
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
        raise TypeError(
            f"{path} uses a mutable container; use tuple or a nested frozen dataclass"
        )

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
    seen: frozenset[type[object]] = frozenset(),
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
        if type(value) is not str:
            raise PayloadRejected("invalid_type", f"{path} must be a string")
        return value
    if annotation is bool:
        if type(value) is not bool:
            raise PayloadRejected("invalid_type", f"{path} must be a boolean")
        return value
    if annotation is int:
        if type(value) is not int:
            raise PayloadRejected("invalid_type", f"{path} must be an integer")
        return value
    if annotation is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise PayloadRejected(
                "invalid_type",
                f"{path} must be a finite number",
            )
        return float(value)
    if annotation is NoneType:
        if value is not None:
            raise PayloadRejected("invalid_type", f"{path} must be null")
        return None

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Literal:
        if value not in arguments or not any(
            type(value) is type(item)
            for item in arguments
        ):
            raise PayloadRejected(
                "invalid_value",
                f"{path} is not one of the declared literal values",
            )
        return value
    if origin is tuple:
        if type(value) is not list:
            raise PayloadRejected("invalid_type", f"{path} must be an array")
        if len(arguments) == 2 and arguments[1] is Ellipsis:
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
                "invalid_length",
                f"{path} must contain exactly {len(arguments)} items",
            )
        return tuple(
            _decode_value(
                item,
                expected,
                path=_path_index(path, index),
            )
            for index, (item, expected) in enumerate(
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
                "invalid_value",
                f"{path} is not a declared enum value",
            ) from error
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _decode_dataclass(value, annotation, path=path)
    raise AssertionError(f"Unsupported annotation reached decoding: {annotation!r}")


def _decode_dataclass(value: object, value_type: type[T], *, path: str) -> T:
    if type(value) is not dict:
        raise PayloadRejected("invalid_type", f"{path} must be an object")
    (declared_fields, hints) = _dataclass_fields(value_type)
    declared = {field.name: field for field in declared_fields}
    extra = set(value) - set(declared)
    if extra:
        raise PayloadRejected(
            "unexpected_field",
            f"{path} contains unexpected fields: {sorted(extra)!r}",
        )
    missing = {
        name
        for (name, field) in declared.items()
        if name not in value
        and field.default is MISSING
        and field.default_factory is MISSING
    }
    if missing:
        raise PayloadRejected(
            "missing_field",
            f"{path} is missing required fields: {sorted(missing)!r}",
        )
    arguments = {
        name: _decode_value(
            item,
            hints[name],
            path=_path_field(path, name),
        )
        for (name, item) in value.items()
    }
    try:
        return value_type(**arguments)
    except CallRejected:
        raise
    except (TypeError, ValueError) as error:
        raise PayloadRejected(
            "invalid_value",
            f"{path} could not construct {value_type.__qualname__}",
        ) from error


def _encode_value(value: object, annotation: object, *, path: str) -> object:
    if annotation is str:
        if type(value) is not str:
            raise TypeError(f"{path} must be str, got {type(value).__qualname__}")
        return value
    if annotation is bool:
        if type(value) is not bool:
            raise TypeError(f"{path} must be bool, got {type(value).__qualname__}")
        return value
    if annotation is int:
        if type(value) is not int:
            raise TypeError(f"{path} must be int, got {type(value).__qualname__}")
        return value
    if annotation is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise TypeError(f"{path} must be a finite float-compatible number")
        return float(value)
    if annotation is NoneType:
        if value is not None:
            raise TypeError(f"{path} must be None")
        return None

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Literal:
        if value not in arguments or not any(
            type(value) is type(item)
            for item in arguments
        ):
            raise TypeError(f"{path} is not one of the declared literal values")
        return value
    if origin is tuple:
        if type(value) is not tuple:
            raise TypeError(f"{path} must be tuple")
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
            for index, (item, expected) in enumerate(
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
        if type(value) is not annotation:
            raise TypeError(f"{path} must be {annotation.__qualname__}")
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
    if type(value) is not value_type:
        raise TypeError(
            f"{path} must be exactly {value_type.__qualname__}, "
            f"got {type(value).__qualname__}"
        )
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
        _validate_dataclass(self.value_type)

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
                    "must name a concrete live-state class"
                )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(field.name for field in fields(self.value_type))

    def bind(self, values: Mapping[str, object]) -> T:
        if not isinstance(values, Mapping):
            raise TypeError("Live scientific states must be supplied as a mapping")
        if set(values) != set(self.names):
            raise ValueError(
                "Live scientific state names differ from the declared state struct"
            )
        hints = _resolved_hints(self.value_type)
        for (name, value) in values.items():
            expected = hints[name]
            if type(value) is not expected:
                raise TypeError(
                    f"Scientific state {name!r} must be exactly "
                    f"{expected.__qualname__}, got {type(value).__qualname__}"
                )
        return self.value_type(**values)
