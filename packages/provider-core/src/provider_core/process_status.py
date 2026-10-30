"""One private, expiring process observation for read-only container health checks.

The caller already owns its directory exclusively. This snapshot neither admits
scientific operations nor proves their completion; those owners keep their own
protocols. Linux process start ticks prevent a recycled PID accepting stale state.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import json
import math
import os
import stat
import time
from pathlib import Path


def _start_ticks(pid):
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    if fields[0] in ("Z", "X"):
        raise ValueError("Process status identifies an exited process.")
    return int(fields[19])


def _private_file(descriptor):
    observed = os.fstat(descriptor)
    if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o600:
        raise PermissionError("Process status requires a caller-owned mode 0600 regular file.")


class ProcessStatus:
    def __init__(self, directory, role):
        self.directory = directory
        self.role = role
        self.pid = os.getpid()
        self.start_ticks = _start_ticks(self.pid)
        self.phase = None
        self.details = None
        self.updated = float("-inf")

    def refresh(self, phase, *, details=None):
        now = time.monotonic()
        if (phase == self.phase and now - self.updated < 5
                and (details == self.details or (details is not None and self.details is not None))):
            return
        value = dict(role=self.role, pid=self.pid, start_ticks=self.start_ticks, updated_monotonic=now, phase=phase)
        if details is not None:
            value["details"] = details
        content = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        pending = self.directory / ".status.pending"
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            _private_file(descriptor)
            os.ftruncate(descriptor, 0)
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(content)
        finally:
            os.close(descriptor)
        os.replace(pending, self.directory / "status.json")
        self.phase, self.details, self.updated = phase, details, now


def read_status(path, role):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _private_file(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(4097)
        if not (len(raw) <= 4096):
            raise ValueError("Process status exceeds its fixed byte limit.")
        value = json.loads(raw)
    finally:
        os.close(descriptor)
    fields = {"role", "pid", "start_ticks", "updated_monotonic", "phase"}
    if type(value) is not dict or set(value) not in (fields, fields | {"details"}):
        raise ValueError("Process status has an unsupported shape.")
    if "details" in value and type(value["details"]) is not dict:
        raise ValueError("Process status details must be an object.")
    if value["role"] != role or any(type(value[name]) is not int or value[name] <= 0 for name in ("pid", "start_ticks")):
        raise ValueError("Process status has a different role or invalid process identity.")
    updated = value["updated_monotonic"]
    if not (type(updated) in (int, float) and math.isfinite(updated) and 0 <= updated):
        raise ValueError("Process status has an invalid timestamp.")
    if _start_ticks(value["pid"]) != value["start_ticks"]:
        raise ValueError("Process status belongs to a different process identity.")
    if value["phase"] not in ("ready", "working", "starting", "stopping", "held", "api-unconfirmed"):
        raise ValueError("Process status has an unsupported readiness phase.")
    return value


def check_status(path, role):
    value = read_status(path, role)
    if not 0 <= time.monotonic() - value["updated_monotonic"] <= 30:
        raise ValueError("Process status observation expired or has an invalid timestamp.")
    if value["phase"] not in ("ready", "working"):
        raise ValueError(f"Process is not ready: {value['phase']}.")
    return value


def main():
    parser = argparse.ArgumentParser(description="Check current private process readiness without opening an operation socket.")
    parser.add_argument("path", type=Path)
    parser.add_argument("role")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--describe", action="store_true")
    display.add_argument("--details", action="store_true")
    options = parser.parse_args()
    try:
        if options.describe:
            value = read_status(options.path, options.role)
            age = time.monotonic() - value["updated_monotonic"]
            if not (0 <= age):
                raise ValueError("Process status observation has a future timestamp.")
            print(f"{value['phase']}|{int(age * 1000)}|{value['pid']}|{value['start_ticks']}")
        else:
            value = check_status(options.path, options.role)
            if options.details:
                if "details" not in value:
                    raise ValueError("Process has not published operator details.")
                age = time.monotonic() - value["updated_monotonic"]
                details = dict(value["details"])
                if "queue_observation_age_seconds" in details:
                    details["queue_observation_age_seconds"] += age
                if details.get("queue") is not None and details["queue"].get("oldest_pending_age_seconds") is not None:
                    details["queue"]["oldest_pending_age_seconds"] += age
                print(json.dumps({"observation_age_seconds": age, **details}, sort_keys=True, allow_nan=False))
    except (OSError, ValueError, KeyError) as cause:
        parser.exit(1, f"Process readiness not confirmed: {cause}\n")


if __name__ == "__main__":
    main()
