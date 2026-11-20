"""One private, expiring process observation for read-only container health checks.

The caller already owns its directory exclusively. This snapshot neither admits
scientific operations nor proves their completion; those owners keep their own
protocols. Linux process start ticks prevent a recycled PID accepting stale state.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import json
import os
import stat
import time
import pathlib

from provider_core.tools import require_nonnegative_number
from provider_core.type_requirements import require_type


def _start_ticks(pid):
    proc_stat_path = f"/proc/{pid}/stat"  # machine-string: the Linux procfs reader opens this exact /proc/<pid>/stat path.
    raw = pathlib.Path(proc_stat_path).read_text()
    fields = raw[raw.rfind(")") + 2:].split()

    if fields[0] in ("Z", "X"):
        raise ValueError("Process status identifies an exited process.")

    return int(fields[19])


def _private_file(descriptor):
    observed = os.fstat(descriptor)

    if (not stat.S_ISREG(observed.st_mode)) or (observed.st_uid != os.geteuid()) or (stat.S_IMODE(observed.st_mode) != 0o600):
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

        if (
            (phase == self.phase) and
            ((now - self.updated) < 5) and
            ((details == self.details) or ((details is not None) and (self.details is not None)))
        ):
            return

        status_record = dict(role=self.role, pid=self.pid, start_ticks=self.start_ticks, updated_monotonic=now, phase=phase)

        if details is not None:
            status_record["details"] = details

        content = json.dumps(status_record, allow_nan=False, separators=(",", ":")).encode()
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
        (self.phase, self.details, self.updated) = (phase, details, now)


def read_status(path, role):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)

    try:
        _private_file(descriptor)

        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(4097)

        if not (len(raw) <= 4096):
            raise ValueError("Process status exceeds its fixed byte limit.")

        status_record = json.loads(raw)
    finally:
        os.close(descriptor)

    fields = { "role", "pid", "start_ticks", "updated_monotonic", "phase" }

    require_type(status_record, dict)

    if not (set(status_record) in (fields, fields | { "details" })):
        raise ValueError("Process status has an unsupported shape.")

    if "details" in status_record:
        value_details = status_record["details"]

        require_type(value_details, dict)

    if status_record["role"] != role:
        raise ValueError("Process status has a different role or invalid process identity.")

    value_pid = status_record['pid']

    require_type(value_pid, int)

    if not (0 < status_record['pid']):
        raise ValueError("Process status has a different role or invalid process identity.")

    value_start_ticks = status_record['start_ticks']

    require_type(value_start_ticks, int)

    if not (0 < status_record['start_ticks']):
        raise ValueError("Process status has a different role or invalid process identity.")

    status_updated_at = status_record["updated_monotonic"]

    require_nonnegative_number(status_updated_at)

    if not (_start_ticks(status_record["pid"]) == status_record["start_ticks"]):
        raise ValueError("Process status belongs to a different process identity.")

    if not (status_record["phase"] in (phases := ("ready", "working", "starting", "stopping", "held", "api-unconfirmed"))):
        raise ValueError(f"{status_record['phase']=} ∉ {phases=}.")

    return status_record


def check_status(path, role):
    status_record = read_status(path, role)

    if not (0 <= (time.monotonic() - status_record["updated_monotonic"]) <= 30):
        raise ValueError("Process status observation expired or has an invalid timestamp.")

    if not (status_record["phase"] in ("ready", "working")):
        process_phase = status_record["phase"]

        raise ValueError(f"Process is not ready: {process_phase = }.")

    return status_record


def main():
    parser = argparse.ArgumentParser(
        description="Check current private process readiness without opening an operation socket.",
    )

    parser.add_argument("path", type=pathlib.Path)
    parser.add_argument("role")
    display = parser.add_mutually_exclusive_group()

    display.add_argument("--describe", action="store_true")
    display.add_argument("--details", action="store_true")
    options = parser.parse_args()

    try:
        if options.describe:
            status_record = read_status(options.path, options.role)
            age = time.monotonic() - status_record["updated_monotonic"]

            if not (0 <= age):
                raise ValueError("Process status observation has a future timestamp.")

            process_phase = status_record["phase"]
            age_milliseconds = int(age * 1000)
            process_id = status_record["pid"]
            start_ticks = status_record["start_ticks"]

            print(
                f"{process_phase}|{age_milliseconds}|{process_id}|{start_ticks}",  # machine-string: shell callers parse this four-field pipe-delimited readiness record.
            )
        else:
            status_record = check_status(options.path, options.role)

            if options.details:
                if not ("details" in status_record):
                    raise ValueError("Process has not published operator details.")

                age = time.monotonic() - status_record["updated_monotonic"]
                details = dict(status_record["details"])

                if "queue_observation_age_seconds" in details:
                    details["queue_observation_age_seconds"] += age

                if (details.get("queue") is not None) and (details["queue"].get("oldest_pending_age_seconds") is not None):
                    details["queue"]["oldest_pending_age_seconds"] += age

                print(json.dumps({ "observation_age_seconds": age, **details }, sort_keys=True, allow_nan=False))
    except (OSError, TypeError, ValueError, KeyError) as cause:
        parser.exit(1, f"Process readiness not confirmed: {cause=!s}\n")


if __name__ == "__main__":
    main()
