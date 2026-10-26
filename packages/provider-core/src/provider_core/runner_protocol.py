"""Bounded lifecycle facts; scientific payloads have their own channel and owner."""

from provider_core.container_guard import require_container

require_container()

import array
import json
import math
import os
import re
import socket
import struct
import time

# Bound the runner control message before parsing or dispatch.
# repo-check: allow-module-constant
MAXIMUM_CONTROL_BYTES = 4096
# The runner control token uses this fixed nonce grammar.
# repo-check: allow-module-constant
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


class LifecycleRejected(RuntimeError):
    """A peer did not supply the required operation identity or cleanup evidence."""

    def __init__(self, message, *, returncode=None, scratch_released=None):
        super().__init__(message)
        self.returncode, self.scratch_released = returncode, scratch_released


class RetirementUnconfirmed(RuntimeError):
    """The supervisor cannot authorize replacement of its native child or scratch."""


def token(value):
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def positive_seconds(value):
    if not (type(value) in (int, float) and math.isfinite(value) and 0 < value):
        raise ValueError("Runner lifecycle budget must be a finite positive number of seconds.")


def _remaining(connection, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Runner lifecycle deadline expired before its complete reply.")
    connection.settimeout(remaining)


def _read(connection, size, deadline):
    result = bytearray()
    while len(result) < size:
        _remaining(connection, deadline)
        chunk, ancillary, flags, _ = connection.recvmsg(size - len(result), 0)
        if flags & socket.MSG_CTRUNC or ancillary:
            raise LifecycleRejected("Unexpected descriptor transfer in a lifecycle frame.")
        if not chunk:
            raise LifecycleRejected("Runner control closed before its complete lifecycle reply.")
        result.extend(chunk)
    return bytes(result)


def send_document(connection, value, *, deadline):
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(raw) > MAXIMUM_CONTROL_BYTES:
        raise LifecycleRejected("Runner lifecycle output exceeds its fixed byte limit.")
    _remaining(connection, deadline)
    connection.sendall(struct.pack("!I", len(raw)) + raw)


def receive_document(connection, *, deadline):
    size = struct.unpack("!I", _read(connection, 4, deadline))[0]
    if not 0 < size <= MAXIMUM_CONTROL_BYTES:
        raise LifecycleRejected("Runner lifecycle reply has an invalid byte count.")
    try:
        value = json.loads(_read(connection, size, deadline))
    except (ValueError, UnicodeError) as cause:
        raise LifecycleRejected("Runner lifecycle reply is not a complete JSON document.") from cause
    if type(value) is not dict:
        raise LifecycleRejected("Runner lifecycle reply must be an object.")
    return value


def send_greeting(connection, value, science, *, deadline):
    # Send rights exactly once. A partial later frame write must not transfer
    # another endpoint, and asyncio buffering must not consume this first byte.
    _remaining(connection, deadline)
    if connection.sendmsg([b"\0"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [science.fileno()]))]) != 1:
        raise LifecycleRejected("Runner could not transfer its scientific endpoint.")
    send_document(connection, value, deadline=deadline)


def receive_greeting(connection, *, deadline):
    descriptors = []
    science = None
    try:
        _remaining(connection, deadline)
        first, ancillary, flags, _ = connection.recvmsg(
            1, socket.CMSG_SPACE(8 * array.array("i").itemsize), socket.MSG_CMSG_CLOEXEC
        )
        unexpected = False
        for level, kind, raw in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array("i")
                values.frombytes(raw[:len(raw) - len(raw) % values.itemsize])
                descriptors.extend(values)
            else:
                unexpected = True
        if first != b"\0" or flags & socket.MSG_CTRUNC or unexpected or len(descriptors) != 1:
            raise LifecycleRejected("Runner greeting requires exactly one scientific endpoint.")
        science = socket.socket(fileno=descriptors[0])
        descriptors.clear()
        if science.family != socket.AF_UNIX or science.type != socket.SOCK_STREAM:
            raise LifecycleRejected("Runner supplied an unsupported scientific endpoint type.")
        value = receive_document(connection, deadline=deadline)
        if (
            set(value) != {"kind", "role", "generation", "operation", "lock"}
            or value["kind"] != "opened" or not token(value["generation"]) or not token(value["operation"])
            or type(value["role"]) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value["role"])
            or type(value["lock"]) is not list or len(value["lock"]) != 2
            or any(type(part) is not int or part < 0 for part in value["lock"])
        ):
            raise LifecycleRejected("Runner greeting has invalid generation, operation or lock identity.")
        return value, science
    except BaseException:
        if science is not None:
            science.close()
        raise
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
