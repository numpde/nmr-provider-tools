"""Small bounded data protocol for the private scientific process channel.

Only SDK records cross the channel. The sender never serializes a live model,
Python callable, state value, or exception object with pickle.
Reconstructed errors retain bounded child locations for protected diagnostics;
the added exception note renders those facts without child source or locals.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import base64
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import json
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
from provider_core.tools import require_finite_number


MAX_FRAME_BYTES = 8 * 1024 * 1024
# Leave room for base64 expansion, SDK records and bounded failure evidence.
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_FAILURE_NODES = 32
MAX_FAILURE_DEPTH = 8
MAX_DIAGNOSTIC_CHARACTERS = 2048
MAX_TRACEBACK_FRAMES = 8
MAX_TRACEBACK_LOCATION_CHARACTERS = 512
MAX_TRACEBACK_LINE = (2 ** 31) - 1


class RemoteScienceError(RuntimeError):
    """Foreign failure evidence, never permission to correct or repeat science."""


@dataclass(frozen=True, slots=True)
class _DecodedErrorEvidence:
    kind: str
    original_type: str
    message: str
    code: str | None
    frames: tuple[tuple[str, str, int], ...]
    frames_omitted: bool
    causes: tuple[_DecodedErrorEvidence, ...]
    cause: _DecodedErrorEvidence | None
    context: _DecodedErrorEvidence | None


_RECORDS = {
    cls.__name__: cls
    for cls in (
        CallRef,
        ProgressEvent,
        ResourceRequirement,
        AllocationRef,
        CleanupObservation,
        Failure,
        Invocation,
        NotAdmitted,
        Observation,
        OperationPolicy,
        OperationRef,
        ReservationRef,
        ResultRef,
        StateRef,
        StateResult,
    )
}

_ENUMS = {
    cls.__name__: cls
    for cls in (
        AcceptanceState,
        AdmissionCode,
        DispatchState,
        Outcome,
        ResourceState,
        ResultState,
    )
}

_ERRORS = {
    cls.__name__: cls
    for cls in (
        CallRejected,
        ScientificRejection,
        StartDeadlineExpired,
        TypeError,
        ValueError,
        OSError,
        RuntimeError,
        TimeoutError,
        PermissionError,
    )
}


def decoded_error_evidence(failure: BaseException) -> _DecodedErrorEvidence | None:
    """Expose decoder-owned facts to protected consumers, which must redact them.

    Exact reconstructed types and a private immutable record distinguish these
    facts from arbitrary exception attributes, properties and mutable notes.
    """

    kind = type(failure)

    if not (
        (kind is RemoteScienceError)
        or (kind is BaseExceptionGroup)
        or (kind is ExceptionGroup)
        or any(kind is admitted_type for admitted_type in _ERRORS.values())
    ):
        return None

    attributes = BaseException.__getattribute__(failure, "__dict__")
    evidence = dict.get(attributes, "_process_error_evidence")

    return evidence if type(evidence) is _DecodedErrorEvidence else None


def _diagnostic_text(diagnostic: str) -> str:
    if len(diagnostic) <= MAX_DIAGNOSTIC_CHARACTERS:
        return diagnostic

    return diagnostic[:MAX_DIAGNOSTIC_CHARACTERS] + " [truncated]"


def _pack_traceback(failure: BaseException) -> dict:
    """Keep innermost child locations without reading source, locals or globals."""

    frames = deque(maxlen=MAX_TRACEBACK_FRAMES)
    frames_omitted = False
    current = failure.__traceback__

    while current is not None:
        code = current.tb_frame.f_code
        (file, function, line) = (code.co_filename, code.co_name, current.tb_lineno)
        current = current.tb_next

        if not (
            (0 < len(file) <= MAX_TRACEBACK_LOCATION_CHARACTERS)
            and (0 < len(function) <= MAX_TRACEBACK_LOCATION_CHARACTERS)
            and (type(line) is int)
            and (0 < line <= MAX_TRACEBACK_LINE)
        ):
            frames_omitted = True

            continue

        if len(frames) == MAX_TRACEBACK_FRAMES:
            frames_omitted = True

        frames.append({"file": file, "function": function, "line": line})

    return {"frames": list(frames), "frames_omitted": frames_omitted}


def _pack_error(failure: BaseException, *, seen: frozenset[int], remaining: list[int]) -> dict:
    original_type = f"{type(failure).__module__}.{type(failure).__qualname__}"  # machine-string: The wire original_type field and reconstructed remote_type retain a dotted Python type identity
    original_type = _diagnostic_text(original_type)
    limited = (id(failure) in seen) or (len(seen) >= MAX_FAILURE_DEPTH) or (remaining[0] == 0)

    if remaining[0]:
        remaining[0] -= 1

    if limited:
        return {
            "error": "remote",
            "original_type": original_type,
            "message": "Further failure evidence omitted: cycle or diagnostic limit",
            "frames": [],
            "frames_omitted": True,
        }

    seen = seen | {id(failure)}
    name = "remote"

    if isinstance(failure, CallRejected):
        name = "ScientificRejection" if isinstance(failure, ScientificRejection) else "CallRejected"
    elif type(failure) in _ERRORS.values():
        name = type(failure).__name__
    elif isinstance(failure, BaseExceptionGroup):
        name = "group"

    if isinstance(failure, (CallRejected, BaseExceptionGroup)):
        message = failure.message
    else:
        try:
            message = str(failure)
        except BaseException as cause:
            message = f"Failure text unavailable: {type(cause)=} raised by __str__"

    error_fields = {
        "error": name,
        "original_type": original_type,
        "message": _diagnostic_text(message),
        **_pack_traceback(failure),
    }

    if isinstance(failure, CallRejected):
        error_fields["code"] = failure.code.value if isinstance(failure.code, Enum) else failure.code

    if name == "group":
        causes = []

        for child in failure.exceptions:
            causes.append(_pack_error(child, seen=seen, remaining=remaining))

            if remaining[0] == 0:
                if len(causes) < len(failure.exceptions):
                    causes.append(
                        {
                            "error": "remote",
                            "original_type": original_type,
                            "message": "Additional independent failures omitted by diagnostic limit",
                            "frames": [],
                            "frames_omitted": True,
                        }
                    )

                break

        error_fields["causes"] = causes

    if failure.__cause__ is not None:
        error_fields["cause"] = _pack_error(failure.__cause__, seen=seen, remaining=remaining)
    elif (failure.__context__ is not None) and (not failure.__suppress_context__):
        error_fields["context"] = _pack_error(failure.__context__, seen=seen, remaining=remaining)

    return error_fields


def pack(sdk_value: object) -> object:
    """Convert one bounded SDK value to a JSON-compatible tree."""

    if (sdk_value is None) or (type(sdk_value) in (str, bool, int, float)):
        return sdk_value

    if isinstance(sdk_value, bytes):
        if not (len(sdk_value) <= MAX_PAYLOAD_BYTES):
            raise ValueError(f"Scientific payload {len(sdk_value)=} should not exceed {MAX_PAYLOAD_BYTES=}")

        return {"bytes": base64.b64encode(sdk_value).decode("ascii")}

    if isinstance(sdk_value, Enum):
        if not (_ENUMS.get(type(sdk_value).__name__) is type(sdk_value)):
            raise TypeError(f"{type(sdk_value)=} should be an admitted scientific process enum type")

        return {"enum": type(sdk_value).__name__, "value": sdk_value.value}

    if isinstance(sdk_value, BaseException):
        return _pack_error(sdk_value, seen=frozenset(), remaining=[MAX_FAILURE_NODES])

    if is_dataclass(sdk_value) and not isinstance(sdk_value, type):
        name = type(sdk_value).__name__

        if not ((name in _RECORDS) and (type(sdk_value) is _RECORDS[name])):
            raise TypeError(f"{type(sdk_value)=} should be an admitted scientific process record type")

        return {
            "record": name,
            "fields": {
                member.name: pack(getattr(sdk_value, member.name))
                for member in fields(sdk_value)
                if member.init
            },
        }

    if isinstance(sdk_value, tuple):
        return {"tuple": [pack(element) for element in sdk_value]}

    if isinstance(sdk_value, Mapping):
        if not all((type(key) is str) for key in sdk_value):
            raise TypeError("Process message mapping keys must be strings")

        return {"mapping": {key: pack(field_value) for (key, field_value) in sdk_value.items()}}

    if isinstance(sdk_value, list):
        return [pack(element) for element in sdk_value]

    raise TypeError(f"{type(sdk_value)=} has no scientific process encoding")


def unpack(wire_value: object) -> object:
    """Reconstruct only admitted SDK records, never arbitrary Python objects."""

    if (wire_value is None) or (type(wire_value) in (str, bool, int, float)):
        if type(wire_value) is float:
            require_finite_number(wire_value)

        return wire_value

    if type(wire_value) is list:
        return [unpack(element) for element in wire_value]

    if not (type(wire_value) is dict):
        raise ValueError(f"Scientific process record {type(wire_value)=} should be dict")

    if set(wire_value) == {"bytes"}:
        payload_bytes = base64.b64decode(wire_value["bytes"], validate=True)

        if not (len(payload_bytes) <= MAX_PAYLOAD_BYTES):
            raise ValueError(f"Scientific payload {len(payload_bytes)=} should not exceed {MAX_PAYLOAD_BYTES=}")

        return payload_bytes

    if (set(wire_value) == {"tuple"}) and (type(wire_value["tuple"]) is list):
        return tuple(unpack(element) for element in wire_value["tuple"])

    if (set(wire_value) == {"mapping"}) and (type(wire_value["mapping"]) is dict):
        return {key: unpack(field_value) for (key, field_value) in wire_value["mapping"].items()}

    if set(wire_value) == {"enum", "value"}:
        return _ENUMS[wire_value["enum"]](wire_value["value"])

    if (set(wire_value) == {"record", "fields"}) and (type(wire_value["fields"]) is dict):
        record_type = _RECORDS[wire_value["record"]]

        if not (set(wire_value["fields"]) == {member.name for member in fields(record_type) if member.init}):
            raise ValueError("Scientific process record fields differ from its declaration")

        return record_type(**{key: unpack(field_value) for (key, field_value) in wire_value["fields"].items()})

    if "error" in wire_value:
        # The sender can append explicit limit markers beyond its retained-node budget.
        return _unpack_error(wire_value, depth=0, remaining=[2 * MAX_FAILURE_NODES])

    raise ValueError("Unknown scientific process record")


def _unpack_error(error_fields: dict, *, depth: int, remaining: list[int]) -> BaseException:
    if not ((depth <= MAX_FAILURE_DEPTH) and (0 < remaining[0])):
        raise ValueError("Scientific process failure graph exceeds diagnostic limits")

    remaining[0] -= 1

    if not ((type(error_fields) is dict) and (type(error_fields.get("error")) is str)):
        raise ValueError("Scientific process failure needs an error record with a string kind")

    required = {"error", "message", "original_type", "frames", "frames_omitted"}
    name = error_fields["error"]

    if name in ("CallRejected", "ScientificRejection"):
        required.add("code")
    elif name == "group":
        required.add("causes")
    elif not ((name == "remote") or (name in _ERRORS)):
        raise ValueError("Unknown scientific process error kind")

    if not ((required <= error_fields.keys()) and (error_fields.keys() <= (required | {"cause", "context"}))):
        raise ValueError("Invalid scientific process error fields")

    if not (("cause" not in error_fields) or ("context" not in error_fields)):
        raise ValueError("Scientific process error has conflicting causal links")

    if not ((type(error_fields["message"]) is str) and (type(error_fields["original_type"]) is str)):
        raise ValueError(
            f"Scientific process descriptions {type(error_fields['message'])=} and "
            f"{type(error_fields['original_type'])=} should both be str"
        )

    if not all(len(error_fields[field]) <= MAX_DIAGNOSTIC_CHARACTERS + len(" [truncated]") for field in ("message", "original_type")):
        raise ValueError("Scientific process descriptions exceed diagnostic limits")

    frames = error_fields["frames"]
    frames_omitted = error_fields["frames_omitted"]

    if not ((type(frames) is list) and (len(frames) <= MAX_TRACEBACK_FRAMES) and (type(frames_omitted) is bool)):
        raise ValueError("Scientific process traceback needs a bounded frame list and a boolean omission marker")

    for location in frames:
        if not ((type(location) is dict) and (set(location) == {"file", "function", "line"})):
            raise ValueError("Scientific process traceback frame must contain only file, function and line")

        (file, function, line) = (location["file"], location["function"], location["line"])

        if not (
            (type(file) is str)
            and (0 < len(file) <= MAX_TRACEBACK_LOCATION_CHARACTERS)
            and (type(function) is str)
            and (0 < len(function) <= MAX_TRACEBACK_LOCATION_CHARACTERS)
            and (type(line) is int)
            and (0 < line <= MAX_TRACEBACK_LINE)
        ):
            raise ValueError("Scientific process traceback locations must have bounded names and positive integer lines")

    group_failures = ()

    if name == "group":
        if not ((type(error_fields["causes"]) is list) and (0 < len(error_fields["causes"]) <= MAX_FAILURE_NODES)):
            raise ValueError("Scientific process error group needs a nonempty bounded failure list")

        group_failures = tuple(_unpack_error(cause, depth=depth + 1, remaining=remaining) for cause in error_fields["causes"])
        failure = BaseExceptionGroup(error_fields["message"], group_failures)
    elif name in ("CallRejected", "ScientificRejection"):
        failure = _ERRORS[name](code=error_fields["code"], message=error_fields["message"])
    elif name in _ERRORS:
        failure = _ERRORS[name](error_fields["message"])
    else:
        original_type = error_fields["original_type"]
        message = error_fields["message"]
        failure = RemoteScienceError(f"Remote scientific failure: {original_type=}; {message=}")

    failure.remote_type = error_fields["original_type"]

    if frames or frames_omitted:
        note_lines = ["Remote traceback (innermost first):"]

        if frames_omitted:
            note_lines.append("Some remote traceback frames omitted by diagnostic limits")

        for location in reversed(frames):
            (file, function, line) = (location["file"], location["function"], location["line"])
            note_lines.append(f"  {file = !a}, {line = }, {function = !a}")

        failure.add_note(_diagnostic_text("\n".join(note_lines)))

    if "cause" in error_fields:
        failure.__cause__ = _unpack_error(error_fields["cause"], depth=depth + 1, remaining=remaining)
    elif "context" in error_fields:
        failure.__context__ = _unpack_error(error_fields["context"], depth=depth + 1, remaining=remaining)

    failure._process_error_evidence = _DecodedErrorEvidence(
        kind=name,
        original_type=error_fields["original_type"],
        message=error_fields["message"],
        code=error_fields["code"] if "code" in error_fields else None,
        frames=tuple((location["file"], location["function"], location["line"]) for location in frames),
        frames_omitted=frames_omitted,
        causes=tuple(child._process_error_evidence for child in group_failures),
        cause=failure.__cause__._process_error_evidence if failure.__cause__ is not None else None,
        context=failure.__context__._process_error_evidence if failure.__context__ is not None else None,
    )

    return failure


def frame(sdk_value: object) -> bytes:
    encoded_message = json.dumps(pack(sdk_value), ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")

    if not (len(encoded_message) <= MAX_FRAME_BYTES):
        raise ValueError(f"Scientific message {len(encoded_message)=} should not exceed {MAX_FRAME_BYTES=}")

    return struct.pack("!I", len(encoded_message)) + encoded_message


def decode_frame(encoded_message: bytes) -> object:
    if not (len(encoded_message) <= MAX_FRAME_BYTES):
        raise ValueError(f"Scientific message {len(encoded_message)=} should not exceed {MAX_FRAME_BYTES=}")

    try:
        return unpack(json.loads(encoded_message.decode("ascii"), object_pairs_hook=_unique_fields))
    except (ValueError, TypeError, LookupError, AttributeError, RecursionError, OverflowError) as cause:
        raise ValueError("Malformed scientific process message") from cause


def _unique_fields(field_pairs) -> dict:
    decoded_fields = {}

    for (key, field_value) in field_pairs:
        if not (key not in decoded_fields):
            raise ValueError("Scientific process object repeats a field")

        decoded_fields[key] = field_value

    return decoded_fields


def _read_exact(sock: socket.socket, count: int, *, deadline: float | None) -> bytes:
    received_bytes = bytearray()

    while len(received_bytes) < count:
        if deadline is not None:
            remaining = deadline - time.monotonic()

            if not (0 < remaining):
                raise TimeoutError(f"Scientific process reply exceeded its original {deadline=}; {remaining=}")

            sock.settimeout(remaining)

        chunk = sock.recv(count - len(received_bytes))

        if not (chunk):
            raise EOFError(f"Scientific process reply stopped at {len(received_bytes)=}; expected {count=}")

        received_bytes.extend(chunk)

    return bytes(received_bytes)


def receive(sock: socket.socket, *, deadline: float | None = None) -> object:
    """Read a reply within one deadline; omitted deadlines keep socket policy."""

    (length,) = struct.unpack("!I", _read_exact(sock, 4, deadline=deadline))

    if not (length <= MAX_FRAME_BYTES):
        raise ValueError(f"Scientific process frame {length=} should not exceed {MAX_FRAME_BYTES=}")

    return decode_frame(_read_exact(sock, length, deadline=deadline))


def send(sock: socket.socket, sdk_value: object) -> None:
    sock.sendall(frame(sdk_value))


async def receive_async(sock: socket.socket) -> object:
    loop = asyncio.get_running_loop()

    async def read_exact(count: int) -> bytes:
        received_bytes = bytearray()

        while len(received_bytes) < count:
            chunk = await loop.sock_recv(sock, count - len(received_bytes))

            if not (chunk):
                raise EOFError(f"Scientific process request stopped at {len(received_bytes)=}; expected {count=}")

            received_bytes.extend(chunk)

        return bytes(received_bytes)

    (length,) = struct.unpack("!I", await read_exact(4))

    if not (length <= MAX_FRAME_BYTES):
        raise ValueError(f"Scientific process frame {length=} should not exceed {MAX_FRAME_BYTES=}")

    return decode_frame(await read_exact(length))


async def send_async(sock: socket.socket, sdk_value: object) -> None:
    await asyncio.get_running_loop().sock_sendall(sock, frame(sdk_value))
