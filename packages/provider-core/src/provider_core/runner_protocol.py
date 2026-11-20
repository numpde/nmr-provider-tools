"""Bounded lifecycle facts; scientific payloads have their own channel and owner."""

from provider_core.container_guard import require_container

require_container()

import array
import json
import os
import re
import reprlib
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
    remaining_seconds = deadline - time.monotonic()

    if remaining_seconds <= 0:
        raise TimeoutError(f"{deadline=} has expired with {remaining_seconds=}")

    connection.settimeout(remaining_seconds)


def _read(connection, expected_frame_bytes, deadline):
    frame_buffer = bytearray()

    while len(frame_buffer) < expected_frame_bytes:
        _remaining(connection, deadline)
        (chunk, ancillary, flags, _) = connection.recvmsg(expected_frame_bytes - len(frame_buffer), 0)

        if not ((flags & socket.MSG_CTRUNC) == 0):
            raise LifecycleRejected(f"{flags=:#x} should exclude {socket.MSG_CTRUNC=:#x}")

        if ancillary:
            raise LifecycleRejected(f"{len(ancillary)=} should be zero in lifecycle frames")

        if not (chunk):
            raise LifecycleRejected(f"Lifecycle control closed at {len(frame_buffer)=}; expected {expected_frame_bytes=}")

        frame_buffer.extend(chunk)

    return bytes(frame_buffer)


def send_document(connection, value, *, deadline):
    control_bytes = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("ascii")

    if not (len(control_bytes) <= MAXIMUM_CONTROL_BYTES):
        raise LifecycleRejected(f"{len(control_bytes)=} should not exceed {MAXIMUM_CONTROL_BYTES=}")

    _remaining(connection, deadline)
    connection.sendall(struct.pack("!I", len(control_bytes)) + control_bytes)


def receive_document(connection, *, deadline):
    control_byte_count = struct.unpack("!I", _read(connection, 4, deadline))[0]

    if not (0 < control_byte_count <= MAXIMUM_CONTROL_BYTES):
        raise LifecycleRejected(f"{control_byte_count=} should be positive and not exceed {MAXIMUM_CONTROL_BYTES=}")

    try:
        lifecycle_message = json.loads(_read(connection, control_byte_count, deadline))
    except (ValueError, UnicodeError) as cause:
        raise LifecycleRejected(f"Cannot decode lifecycle reply with {control_byte_count=}") from cause

    require_type(lifecycle_message, dict, error_factory=LifecycleRejected)

    return lifecycle_message


def send_greeting(connection, value, science, *, deadline):
    # Send rights exactly once. A partial later frame write must not transfer
    # another endpoint, and asyncio buffering must not consume this first byte.
    _remaining(connection, deadline)

    transferred_greeting_bytes = connection.sendmsg([b"\0"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [science.fileno()]))])

    if not (transferred_greeting_bytes == (expected_greeting_bytes := 1)):
        raise LifecycleRejected(f"{transferred_greeting_bytes=} ≠ {expected_greeting_bytes=}")

    send_document(connection, value, deadline=deadline)


def receive_greeting(connection, *, deadline):
    descriptors = []
    science = None

    try:
        _remaining(connection, deadline)

        (greeting_byte, ancillary, flags, _) = connection.recvmsg(
            1,
            socket.CMSG_SPACE(8 * array.array("i").itemsize),
            socket.MSG_CMSG_CLOEXEC
        )

        unexpected_ancillary_types = []

        for (level, kind, raw) in ancillary:
            if (level == socket.SOL_SOCKET) and (kind == socket.SCM_RIGHTS):
                received_fds = array.array("i")

                received_fds.frombytes(raw[:len(raw) - len(raw) % received_fds.itemsize])
                descriptors.extend(received_fds)
            else:
                unexpected_ancillary_types.append((level, kind))

        if not (greeting_byte == (expected_greeting_byte := b"\0")):
            raise LifecycleRejected(f"{greeting_byte=} ≠ {expected_greeting_byte=}")

        if not ((flags & socket.MSG_CTRUNC) == 0):
            raise LifecycleRejected(f"{flags=:#x} should exclude {socket.MSG_CTRUNC=:#x}")

        if not (unexpected_ancillary_types == []):
            raise LifecycleRejected(f"{unexpected_ancillary_types=} should be empty")

        if not (len(descriptors) == (expected_science_fd_count := 1)):
            raise LifecycleRejected(f"{len(descriptors)=} ≠ {expected_science_fd_count=}")

        science = socket.socket(fileno=descriptors[0])

        descriptors.clear()

        if not (science.family == socket.AF_UNIX):
            raise LifecycleRejected(f"{science.family=} ≠ {socket.AF_UNIX=}")

        if not (science.type == socket.SOCK_STREAM):
            raise LifecycleRejected(f"{science.type=} ≠ {socket.SOCK_STREAM=}")

        greeting_message = receive_document(connection, deadline=deadline)

        if not (set(greeting_message) == (expected_greeting_fields := {"kind", "role", "generation", "operation", "lock"})):
            raise LifecycleRejected(f"{reprlib.repr(set(greeting_message))=} ≠ {expected_greeting_fields=}")

        if not (greeting_message["kind"] == (expected_greeting_kind := "opened")):
            raise LifecycleRejected(f"{reprlib.repr(greeting_message['kind'])=} ≠ {expected_greeting_kind=}")

        runner_generation = greeting_message["generation"]

        if not token(runner_generation):
            raise LifecycleRejected(f"{reprlib.repr(runner_generation)=} should match {_TOKEN.pattern=}")

        runner_operation = greeting_message["operation"]

        if not token(runner_operation):
            raise LifecycleRejected(f"{reprlib.repr(runner_operation)=} should match {_TOKEN.pattern=}")

        role = greeting_message["role"]

        require_type(role, str, error_factory=LifecycleRejected)

        if not re.fullmatch(pattern := r"[a-z][a-z0-9_-]{0,47}", role):
            raise LifecycleRejected(f"{role=} should match {pattern=}")

        lock_identity = greeting_message["lock"]

        require_type(lock_identity, list, error_factory=LifecycleRejected)

        if not (len(lock_identity) == (expected_lock_parts := 2)):
            raise LifecycleRejected(f"{len(lock_identity)=} ≠ {expected_lock_parts=}")

        for lock_identity_component in lock_identity:
            require_type(lock_identity_component, int, error_factory=LifecycleRejected)

            if not (0 <= lock_identity_component):
                raise LifecycleRejected(f"{lock_identity_component=} should be nonnegative")

        return (greeting_message, science)
    except BaseException:
        if science is not None:
            science.close()

        raise
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
