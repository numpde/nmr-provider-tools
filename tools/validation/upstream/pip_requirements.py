"""Decode the pip requirements syntax used by the guarded hash-lock profile."""

from tools.container_guard import require_container

require_container()

import re
import reprlib


def parse_lock_line(raw: bytes) -> tuple[str, str] | str | None:
    """Read a pin or hash from one physical line, skipping layout and comments."""

    lock_line = raw.decode().strip().removesuffix("\\").rstrip()

    if (not lock_line) or lock_line.startswith("#"):
        return None

    hash_pattern = r"--hash=sha256:([0-9a-f]{64})"

    if match := re.fullmatch(hash_pattern, lock_line):
        return match[1]

    pin_pattern = r"([A-Za-z0-9_.-]+)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)"
    match = re.fullmatch(pin_pattern, lock_line)

    if match is None:
        raise ValueError(f"{reprlib.repr(lock_line)=} should match {pin_pattern=} or {hash_pattern=}")

    return (re.sub(r"[-_.]+", "-", match[1]).lower(), match[2])
