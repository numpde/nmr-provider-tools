"""Small bounded data protocol for the private scientific process channel.

Only SDK records cross the channel. The sender never serializes a live model,
Python callable, state value, or exception object with pickle.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
import json
import math
import socket
import struct
import time

from provider_core.contribution import CallRef, ProgressEvent, ResourceRequirement
from provider_core.contribution_errors import CallRejected, ScientificRejection
from provider_core.hosting import (
    AcceptanceState,
    AdmissionCode,
    AllocationRef,
    CleanupObservation,
    DispatchState,
    Failure,
    Invocation,
    NotAdmitted,
    Observation,
    OperationPolicy,
    OperationRef,
    Outcome,
    ReservationRef,
    ResourceState,
    ResultRef,
    ResultState,
    StartDeadlineExpired,
    StateRef,
    StateResult,
)


MAX_FRAME_BYTES = 8 * 1024 * 1024
# Leave room for base64 expansion, SDK records and bounded failure evidence.
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_FAILURE_NODES = 32
MAX_FAILURE_DEPTH = 8
MAX_DIAGNOSTIC_CHARACTERS = 2048


class RemoteScienceError(RuntimeError):
    """Foreign failure evidence, never permission to correct or repeat science."""


_RECORDS = {
    cls.__name__: cls
    for cls in (
        CallRef, ProgressEvent, ResourceRequirement, AllocationRef,
        CleanupObservation, Failure, Invocation, NotAdmitted, Observation,
        OperationPolicy, OperationRef, ReservationRef, ResultRef, StateRef,
        StateResult,
    )
}

_ENUMS = {
    cls.__name__: cls
    for cls in (
        AcceptanceState, AdmissionCode, DispatchState, Outcome,
        ResourceState, ResultState,
    )
}

_ERRORS = {
    cls.__name__: cls
    for cls in (
        CallRejected, ScientificRejection, StartDeadlineExpired,
        TypeError, ValueError, OSError, RuntimeError, TimeoutError,
        PermissionError,
    )
}


def _diagnostic_text(value: str) -> str:
    if len(value) <= MAX_DIAGNOSTIC_CHARACTERS:
        return value

    return value[:MAX_DIAGNOSTIC_CHARACTERS] + " [truncated]"


def _pack_error(value: BaseException, *, seen: frozenset[int], remaining: list[int]) -> dict:
    original_type = _diagnostic_text(f"{type(value).__module__}.{type(value).__qualname__}")
    limited = (id(value) in seen) or (len(seen) >= MAX_FAILURE_DEPTH) or (remaining[0] == 0)

    if remaining[0]:
        remaining[0] -= 1

    if limited:
        return {
            "error": "remote",
            "original_type": original_type,
            "message": "Further failure evidence omitted: cycle or diagnostic limit",
        }

    seen = seen | {id(value)}
    name = "remote"

    if isinstance(value, CallRejected):
        name = "ScientificRejection" if isinstance(value, ScientificRejection) else "CallRejected"
    elif type(value) in _ERRORS.values():
        name = type(value).__name__
    elif isinstance(value, BaseExceptionGroup):
        name = "group"

    if isinstance(value, (CallRejected, BaseExceptionGroup)):
        message = value.message
    else:
        try:
            message = str(value)
        except BaseException as cause:
            message = f"Failure text unavailable: {type(cause).__module__}.{type(cause).__qualname__} raised by __str__"

    error = {
        "error": name,
        "original_type": original_type,
        "message": _diagnostic_text(message),
    }

    if isinstance(value, CallRejected):
        error["code"] = value.code.value if isinstance(value.code, Enum) else value.code

    if name == "group":
        causes = []

        for child in value.exceptions:
            causes.append(_pack_error(child, seen=seen, remaining=remaining))

            if remaining[0] == 0:
                if len(causes) < len(value.exceptions):
                    causes.append({
                        "error": "remote",
                        "original_type": original_type,
                        "message": "Additional independent failures omitted by diagnostic limit",
                    })

                break

        error["causes"] = causes

    if value.__cause__ is not None:
        error["cause"] = _pack_error(value.__cause__, seen=seen, remaining=remaining)
    elif (value.__context__ is not None) and (not value.__suppress_context__):
        error["context"] = _pack_error(value.__context__, seen=seen, remaining=remaining)

    return error


def pack(value: object) -> object:
    """Convert one bounded SDK value to a JSON-compatible tree."""
    if value is None or type(value) in (str, bool, int, float):
        return value

    if isinstance(value, bytes):
        if len(value) > MAX_PAYLOAD_BYTES:
            raise ValueError("Scientific process payload exceeds the supported byte limit")

        return {"bytes": base64.b64encode(value).decode("ascii")}

    if isinstance(value, Enum):
        if _ENUMS.get(type(value).__name__) is not type(value):
            raise TypeError(f"No process encoding for {type(value).__name__}")

        return {"enum": type(value).__name__, "value": value.value}

    if isinstance(value, BaseException):
        return _pack_error(value, seen=frozenset(), remaining=[MAX_FAILURE_NODES])

    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__

        if name not in _RECORDS or type(value) is not _RECORDS[name]:
            raise TypeError(f"No process encoding for {type(value).__name__}")

        return {"record": name, "fields": {member.name: pack(getattr(value, member.name)) for member in fields(value) if member.init}}

    if isinstance(value, tuple):
        return {"tuple": [pack(item) for item in value]}

    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise TypeError("Process message mapping keys must be strings")

        return {"mapping": {key: pack(item) for key, item in value.items()}}

    if isinstance(value, list):
        return [pack(item) for item in value]

    raise TypeError(f"No process encoding for {type(value).__name__}")


def unpack(value: object) -> object:
    """Reconstruct only admitted SDK records, never arbitrary Python objects."""
    if value is None or type(value) in (str, bool, int, float):
        if (type(value) is float) and (not math.isfinite(value)):
            raise ValueError("Scientific process numbers must be finite")

        return value

    if type(value) is list:
        return [unpack(item) for item in value]

    if type(value) is not dict:
        raise ValueError("Invalid scientific process value")

    if set(value) == {"bytes"}:
        result = base64.b64decode(value["bytes"], validate=True)

        if len(result) > MAX_PAYLOAD_BYTES:
            raise ValueError("Scientific process payload exceeds the supported byte limit")

        return result

    if set(value) == {"tuple"} and type(value["tuple"]) is list:
        return tuple(unpack(item) for item in value["tuple"])

    if set(value) == {"mapping"} and type(value["mapping"]) is dict:
        return {key: unpack(item) for key, item in value["mapping"].items()}

    if set(value) == {"enum", "value"}:
        return _ENUMS[value["enum"]](value["value"])

    if set(value) == {"record", "fields"} and type(value["fields"]) is dict:
        record = _RECORDS[value["record"]]

        if set(value["fields"]) != {member.name for member in fields(record) if member.init}:
            raise ValueError("Scientific process record fields differ from its declaration")

        return record(**{key: unpack(item) for key, item in value["fields"].items()})

    if "error" in value:
        return _unpack_error(value)

    raise ValueError("Unknown scientific process record")


def _unpack_error(value: dict) -> BaseException:
    required = {"error", "message", "original_type"}
    name = value["error"]

    if name in ("CallRejected", "ScientificRejection"):
        required.add("code")
    elif name == "group":
        required.add("causes")
    elif name != "remote" and name not in _ERRORS:
        raise ValueError("Unknown scientific process error kind")

    if not required <= value.keys() or value.keys() - (required | {"cause", "context"}):
        raise ValueError("Invalid scientific process error fields")

    if "cause" in value and "context" in value:
        raise ValueError("Scientific process error has conflicting causal links")

    if type(value["message"]) is not str or type(value["original_type"]) is not str:
        raise ValueError("Scientific process error descriptions must be strings")

    if name == "group":
        if type(value["causes"]) is not list:
            raise ValueError("Scientific process error group needs a list of causes")

        result = BaseExceptionGroup(value["message"], [unpack(error) for error in value["causes"]])
    elif name in ("CallRejected", "ScientificRejection"):
        result = _ERRORS[name](code=value["code"], message=value["message"])
    elif name in _ERRORS:
        result = _ERRORS[name](value["message"])
    else:
        result = RemoteScienceError(f"{value['original_type']}: {value['message']}")

    result.remote_type = value["original_type"]

    if "cause" in value:
        result.__cause__ = unpack(value["cause"])
    elif "context" in value:
        result.__context__ = unpack(value["context"])

    return result


def frame(value: object) -> bytes:
    payload = json.dumps(pack(value), ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")

    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("Scientific process message exceeds the channel limit")

    return struct.pack("!I", len(payload)) + payload


def decode_frame(data: bytes) -> object:
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("Scientific process message exceeds the channel limit")

    try:
        return unpack(json.loads(data.decode("ascii"), object_pairs_hook=_unique_fields))
    except (ValueError, TypeError, LookupError, AttributeError, RecursionError, OverflowError) as cause:
        raise ValueError("Malformed scientific process message") from cause


def _unique_fields(pairs) -> dict:
    value = {}

    for key, item in pairs:
        if key in value:
            raise ValueError("Scientific process object repeats a field")

        value[key] = item

    return value


def _read_exact(sock: socket.socket, count: int, *, deadline: float | None) -> bytes:
    result = bytearray()

    while len(result) < count:
        if deadline is not None:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise TimeoutError("Scientific process reply exceeded its original deadline")

            sock.settimeout(remaining)

        part = sock.recv(count - len(result))

        if not part:
            raise EOFError("Scientific process connection closed before a complete reply")

        result.extend(part)

    return bytes(result)


def receive(sock: socket.socket, *, deadline: float | None = None) -> object:
    """Read a reply within one deadline; omitted deadlines keep socket policy."""
    (length,) = struct.unpack("!I", _read_exact(sock, 4, deadline=deadline))

    if length > MAX_FRAME_BYTES:
        raise ValueError("Scientific process frame exceeds the channel limit")

    return decode_frame(_read_exact(sock, length, deadline=deadline))


def send(sock: socket.socket, value: object) -> None:
    sock.sendall(frame(value))


async def receive_async(sock: socket.socket) -> object:
    loop = asyncio.get_running_loop()

    async def read_exact(count: int) -> bytes:
        data = bytearray()

        while len(data) < count:
            part = await loop.sock_recv(sock, count - len(data))

            if not part:
                raise EOFError("Scientific process connection closed before a complete request")

            data.extend(part)

        return bytes(data)

    (length,) = struct.unpack("!I", await read_exact(4))

    if length > MAX_FRAME_BYTES:
        raise ValueError("Scientific process frame exceeds the channel limit")

    return decode_frame(await read_exact(length))


async def send_async(sock: socket.socket, value: object) -> None:
    await asyncio.get_running_loop().sock_sendall(sock, frame(value))
