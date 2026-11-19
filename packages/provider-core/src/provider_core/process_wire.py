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
import socket
import struct

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


class RemoteScienceError(RuntimeError):
    """A foreign failure with preserved phase and text, never input rejection."""


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


def pack(value: object) -> object:
    """Convert one bounded SDK value to a JSON-compatible tree."""
    if value is None or type(value) in (str, bool, int, float):
        return value

    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}

    if isinstance(value, Enum):
        return {"enum": type(value).__name__, "value": value.value}

    if isinstance(value, BaseException):
        if isinstance(value, BaseExceptionGroup):
            return {
                "error": "group",
                "message": str(value),
                "causes": [pack(error) for error in value.exceptions],
            }

        name = type(value).__name__

        if isinstance(value, CallRejected):
            # A provider may declare further scientific or payload rejections.
            # The portable semantic contract is CallRejected, not a Python
            # subclass inventory installed separately in the parent.
            name = "ScientificRejection" if type(value) is ScientificRejection else "CallRejected"

        error = {"error": name if name in _ERRORS else "remote", "message": str(value)}

        if isinstance(value, CallRejected):
            error["code"] = value.code.value if isinstance(value.code, Enum) else value.code

        if name not in _ERRORS:
            error["original_type"] = f"{type(value).__module__}.{type(value).__qualname__}"

        return error

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
        return value

    if type(value) is list:
        return [unpack(item) for item in value]

    if type(value) is not dict:
        raise ValueError("Invalid scientific process value")

    if "bytes" in value:
        return base64.b64decode(value["bytes"], validate=True)

    if "tuple" in value:
        return tuple(unpack(item) for item in value["tuple"])

    if "mapping" in value:
        return {key: unpack(item) for key, item in value["mapping"].items()}

    if "enum" in value:
        return _ENUMS[value["enum"]](value["value"])

    if "record" in value:
        return _RECORDS[value["record"]](**{key: unpack(item) for key, item in value["fields"].items()})

    if "error" in value:
        name = value["error"]

        if name == "group":
            causes = [unpack(error) for error in value["causes"]]

            if all(isinstance(error, Exception) for error in causes):
                return ExceptionGroup(value["message"], causes)

            return BaseExceptionGroup(value["message"], causes)

        if name in ("CallRejected", "ScientificRejection"):
            return _ERRORS[name](code=value["code"], message=value["message"])

        if name in _ERRORS:
            return _ERRORS[name](value["message"])

        return RemoteScienceError(f"{value.get('original_type', 'Foreign error')}: {value['message']}")

    raise ValueError("Unknown scientific process record")


def frame(value: object) -> bytes:
    payload = json.dumps(pack(value), ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")

    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("Scientific process message exceeds the channel limit")

    return struct.pack("!I", len(payload)) + payload


def decode_frame(data: bytes) -> object:
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("Scientific process message exceeds the channel limit")

    return unpack(json.loads(data.decode("ascii")))


def _read_exact(sock: socket.socket, count: int) -> bytes:
    result = bytearray()

    while len(result) < count:
        part = sock.recv(count - len(result))

        if not part:
            raise EOFError("Scientific process connection closed before a complete reply")

        result.extend(part)

    return bytes(result)


def receive(sock: socket.socket) -> object:
    (length,) = struct.unpack("!I", _read_exact(sock, 4))

    if length > MAX_FRAME_BYTES:
        raise ValueError("Scientific process frame exceeds the channel limit")

    return decode_frame(_read_exact(sock, length))


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
