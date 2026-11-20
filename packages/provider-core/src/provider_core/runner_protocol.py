"""Bounded lifecycle facts; scientific payloads have their own channel and owner."""

from provider_core.container_guard import require_container

require_container()

import array
import json
import os
import re
import socket
import struct
import time

from provider_core.type_requirements import require_type

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
        (self.returncode, self.scratch_released) = (returncode, scratch_released)


class RetirementUnconfirmed(RuntimeError):
    """The supervisor cannot authorize replacement of its native child or scratch."""


def token(value):
    return (type(value) is str) and (_TOKEN.fullmatch(value) is not None)


def _remaining(connection, deadline):
    remaining = deadline - time.monotonic()

    if remaining <= 0:
        raise TimeoutError("Runner lifecycle deadline expired before its complete reply.")

    connection.settimeout(remaining)


def _read(connection, size, deadline):
    frame_buffer = bytearray()

    while len(frame_buffer) < size:
        _remaining(connection, deadline)
        (chunk, ancillary, flags, _) = connection.recvmsg(size - len(frame_buffer), 0)

        if (flags & socket.MSG_CTRUNC) or ancillary:
            raise LifecycleRejected("Unexpected descriptor transfer in a lifecycle frame.")

        if not (chunk):
            raise LifecycleRejected("Runner control closed before its complete lifecycle reply.")

        frame_buffer.extend(chunk)

    return bytes(frame_buffer)


def send_document(connection, value, *, deadline):
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("ascii")

    if len(raw) > MAXIMUM_CONTROL_BYTES:
        raise LifecycleRejected("Runner lifecycle output exceeds its fixed byte limit.")

    _remaining(connection, deadline)
    connection.sendall(struct.pack("!I", len(raw)) + raw)


def receive_document(connection, *, deadline):
    size = struct.unpack("!I", _read(connection, 4, deadline))[0]

    if not (0 < size <= MAXIMUM_CONTROL_BYTES):
        raise LifecycleRejected("Runner lifecycle reply has an invalid byte count.")

    try:
        lifecycle_message = json.loads(_read(connection, size, deadline))
    except (ValueError, UnicodeError) as cause:
        raise LifecycleRejected("Runner lifecycle reply is not a complete JSON document.") from cause

    require_type(lifecycle_message, dict, error_factory=LifecycleRejected)

    return lifecycle_message


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

        (first, ancillary, flags, _) = connection.recvmsg(
            1,
            socket.CMSG_SPACE(8 * array.array("i").itemsize),
            socket.MSG_CMSG_CLOEXEC
        )

        unexpected = False

        for (level, kind, raw) in ancillary:
            if (level == socket.SOL_SOCKET) and (kind == socket.SCM_RIGHTS):
                received_fds = array.array("i")

                received_fds.frombytes(raw[:len(raw) - len(raw) % received_fds.itemsize])
                descriptors.extend(received_fds)
            else:
                unexpected = True

        if (first != b"\0") or (flags & socket.MSG_CTRUNC) or unexpected or (len(descriptors) != 1):
            raise LifecycleRejected("Runner greeting requires exactly one scientific endpoint.")

        science = socket.socket(fileno=descriptors[0])

        descriptors.clear()

        if (science.family != socket.AF_UNIX) or (science.type != socket.SOCK_STREAM):
            raise LifecycleRejected("Runner supplied an unsupported scientific endpoint type.")

        greeting_message = receive_document(connection, deadline=deadline)

        if not (
            (set(greeting_message) == {"kind", "role", "generation", "operation", "lock"}) and
            (greeting_message["kind"] == "opened") and
            token(greeting_message["generation"]) and
            token(greeting_message["operation"])
        ):
            raise LifecycleRejected("Runner greeting has invalid generation, operation or lock identity.")

        role = greeting_message["role"]

        require_type(role, str, error_factory=LifecycleRejected)

        if not re.fullmatch(pattern := r"[a-z][a-z0-9_-]{0,47}", role):
            raise LifecycleRejected(f"Runner {role=} does not follow {pattern=}.")

        lock = greeting_message["lock"]

        require_type(lock, list, error_factory=LifecycleRejected)

        if len(lock) != 2:
            raise LifecycleRejected("Runner greeting has invalid generation, operation or lock identity.")

        for part in lock:
            require_type(part, int, error_factory=LifecycleRejected)

            if part < 0:
                raise LifecycleRejected("Runner greeting has invalid generation, operation or lock identity.")

        return (greeting_message, science)
    except BaseException:
        if science is not None:
            science.close()

        raise
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
