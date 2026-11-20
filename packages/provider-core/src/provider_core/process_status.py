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
import reprlib
import stat
import time
import pathlib

from provider_core.tools import require_nonnegative_number
from provider_core.type_requirements import require_type


def _start_ticks(pid):
    proc_stat_path = f"/proc/{pid}/stat"  # machine-string: the Linux procfs reader opens this exact /proc/<pid>/stat path.
    proc_stat_text = pathlib.Path(proc_stat_path).read_text()
    proc_stat_fields = proc_stat_text[proc_stat_text.rfind(")") + 2:].split()
    process_state = proc_stat_fields[0]

    if not (process_state not in ("Z", "X")):
        raise ValueError(f"{pid=} has exited with {process_state=}")

    return int(proc_stat_fields[19])


def _private_file(status_fd):
    status_stat = os.fstat(status_fd)

    if not stat.S_ISREG(file_mode := status_stat.st_mode):
        raise PermissionError(f"{status_fd=} has bad {file_mode=}")

    effective_uid = os.geteuid()

    if not (status_stat.st_uid == effective_uid):
        raise PermissionError(f"{status_fd=}; {status_stat.st_uid=} ≠ {effective_uid=}")

    file_permissions = stat.S_IMODE(file_mode)

    if not (file_permissions == (required_permissions := 0o600)):
        raise PermissionError(f"{status_fd=}; {file_permissions=:#o} ≠ {required_permissions=:#o}")


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
            max_status_bytes = 4096
            status_bytes = source.read(max_status_bytes + 1)

        if not (len(status_bytes) <= max_status_bytes):
            raise ValueError(f"{len(status_bytes)=} should not exceed {max_status_bytes=}")

        status_record = json.loads(status_bytes)
    finally:
        os.close(descriptor)

    required_status_fields = { "role", "pid", "start_ticks", "updated_monotonic", "phase" }

    require_type(status_record, dict)

    if not (set(status_record) in (required_status_fields, required_status_fields | { "details" })):
        raise ValueError(f"{reprlib.repr(set(status_record))=} should contain only {required_status_fields=} and optional 'details'")

    if "details" in status_record:
        require_type(status_record["details"], dict)

    if not (status_record["role"] == role):
        raise ValueError(f"{reprlib.repr(status_record['role'])=} ≠ {reprlib.repr(role)=}")

    status_pid = status_record['pid']

    require_type(status_pid, int)

    if not (0 < status_pid):
        raise ValueError(f"{status_pid=} should be positive")

    status_start_ticks = status_record['start_ticks']

    require_type(status_start_ticks, int)

    if not (0 < status_start_ticks):
        raise ValueError(f"{status_start_ticks=} should be positive")

    status_updated_at = status_record["updated_monotonic"]

    require_nonnegative_number(status_updated_at)

    observed_start_ticks = _start_ticks(status_pid)

    if not (observed_start_ticks == status_start_ticks):
        raise ValueError(f"{status_pid=}; {observed_start_ticks=} ≠ {status_start_ticks=}")

    if not (status_record["phase"] in (phases := ("ready", "working", "starting", "stopping", "held", "api-unconfirmed"))):
        raise ValueError(f"{status_record['phase']=} ∉ {phases=}")

    return status_record


def check_status(path, role):
    status_record = read_status(path, role)

    observation_age_seconds = time.monotonic() - status_record["updated_monotonic"]
    max_observation_age_seconds = 30

    if not (0 <= observation_age_seconds <= max_observation_age_seconds):
        raise ValueError(f"{observation_age_seconds=} should be nonnegative and not exceed {max_observation_age_seconds=}")

    if not (status_record["phase"] in (ready_phases := ("ready", "working"))):
        raise ValueError(f"{status_record['phase']=} ∉ {ready_phases=}")

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
            observation_age_seconds = time.monotonic() - status_record["updated_monotonic"]

            if not (0 <= observation_age_seconds):
                raise ValueError(f"{observation_age_seconds=} should be nonnegative")

            process_phase = status_record["phase"]
            age_milliseconds = int(observation_age_seconds * 1000)
            process_id = status_record["pid"]
            start_ticks = status_record["start_ticks"]

            print(
                f"{process_phase}|{age_milliseconds}|{process_id}|{start_ticks}",  # machine-string: shell callers parse this four-field pipe-delimited readiness record.
            )
        else:
            status_record = check_status(options.path, options.role)

            if options.details:
                if not ("details" in status_record):
                    raise ValueError(f"{status_record.keys()=} should include 'details'")

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
