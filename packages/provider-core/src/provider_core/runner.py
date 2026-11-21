"""Serial fresh-child ownership with a separate lifecycle control channel.

Adapted from NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/owner_session_supervisor.py: held-directory endpoint admission,
exclusive lease, epoll/pidfd observation and bounded TERM/KILL/reap. Here the
lease is inherited by the child and science uses an independent socketpair.
The binding supplies fixed argv; no peer can select an executable or deadline.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import fcntl
import logging
import os
import reprlib
import select
import signal
import socket
import stat
import subprocess
import time
import uuid
import pathlib

from provider_core.runner_protocol import RetirementUnconfirmed, send_document, send_greeting
from provider_core.tools import require_positive_number
from provider_core.process_status import ProcessStatus

# Use a fixed diagnostic role for the reusable session owner.
# repo-check: allow-module-constant
LOGGER = logging.getLogger("provider_core.runner")

# One fixed lock leaf owns the exclusive runner session.
# repo-check: allow-module-constant
LOCK_NAME = ".owner-session.lock"


def _private_directory(directory_path):
    if (not directory_path.is_absolute()) or (directory_path.resolve() != directory_path):
        raise ValueError(f"{reprlib.repr(directory_path)=} should be absolute and canonical")

    directory_fd = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    try:
        directory_metadata = os.fstat(directory_fd)

        expected_directory_mode = 0o700

        if (directory_metadata.st_uid != (caller_uid := os.geteuid())) or (stat.S_IMODE(directory_metadata.st_mode) != expected_directory_mode):
            raise PermissionError(
                f"{reprlib.repr(directory_path)=} should be owned by {caller_uid=} with {expected_directory_mode=:#o}; "
                f"{directory_metadata.st_uid=}, {stat.S_IMODE(directory_metadata.st_mode)=:#o}"
            )

        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _lease(parent):
    lease_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)

    try:
        lease_metadata = os.fstat(lease_fd)

        if not stat.S_ISREG(lease_metadata.st_mode):
            raise PermissionError(f"{lease_fd=} should reference a regular file; {lease_metadata.st_mode=:#o}")

        expected_uid = os.geteuid()

        if not (lease_metadata.st_uid == expected_uid):
            raise PermissionError(f"{lease_metadata.st_uid=} should match {expected_uid=}")

        expected_lease_mode = 0o600

        if not (stat.S_IMODE(lease_metadata.st_mode) == expected_lease_mode):
            raise PermissionError(f"{stat.S_IMODE(lease_metadata.st_mode)=:#o} should match {expected_lease_mode=:#o}")

        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        return (lease_fd, (lease_metadata.st_dev, lease_metadata.st_ino))
    except BaseException:
        os.close(lease_fd)
        raise


def _unlink_socket(parent, name, identity=None):
    try:
        socket_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return

    if not stat.S_ISSOCK(socket_metadata.st_mode):
        raise PermissionError(f"{reprlib.repr(name)=} should reference a socket; {socket_metadata.st_mode=:#o}")

    expected_uid = os.geteuid()

    if not (socket_metadata.st_uid == expected_uid):
        raise PermissionError(f"{socket_metadata.st_uid=} should match {expected_uid=}")

    expected_socket_mode = 0o600

    if not (stat.S_IMODE(socket_metadata.st_mode) == expected_socket_mode):
        raise PermissionError(f"{stat.S_IMODE(socket_metadata.st_mode)=:#o} should match {expected_socket_mode=:#o}")

    if identity is not None:
        socket_identity = (socket_metadata.st_dev, socket_metadata.st_ino)

        if not (socket_identity == identity):
            raise PermissionError(f"{socket_identity=} should match {identity=}")

    os.unlink(name, dir_fd=parent)


def _terminate(worker, terminate_seconds, kill_seconds):
    if worker.poll() is not None:
        return worker.returncode

    worker.terminate()

    try:
        return worker.wait(timeout=terminate_seconds)
    except subprocess.TimeoutExpired:
        worker.kill()

    try:
        return worker.wait(timeout=kill_seconds)
    except subprocess.TimeoutExpired as cause:
        raise RetirementUnconfirmed("Native child did not exit after TERM/KILL; replacement remains blocked.") from cause


def _watch(worker, control, shutdown, deadline, status):
    pidfd = os.pidfd_open(worker.pid)

    try:
        with select.epoll() as events:
            events.register(pidfd, select.EPOLLIN)
            events.register(control.fileno(), select.EPOLLRDHUP | select.EPOLLHUP | select.EPOLLERR)
            events.register(shutdown, select.EPOLLIN)

            while True:
                status.refresh("working")
                wait = 5

                if deadline is not None:
                    wait = min(wait, max(0, deadline - time.monotonic()))

                observed = dict(events.poll(wait))

                if shutdown in observed:
                    return "shutdown"

                if pidfd in observed:
                    worker.wait()

                    return "exited"

                if control.fileno() in observed:
                    return "owner_lost"

                if (deadline is not None) and (time.monotonic() >= deadline):
                    return "deadline"
    finally:
        os.close(pidfd)


def _remove_scratch(parent, name, deadline):
    """Delete only the reaped child's tree, checking the bound between entries."""

    cleanup_observed_at = time.monotonic()

    if cleanup_observed_at >= deadline:
        raise TimeoutError(f"{cleanup_observed_at=} should precede {deadline=} while removing {reprlib.repr(name)=}")

    observed = os.stat(name, dir_fd=parent, follow_symlinks=False)

    if not stat.S_ISDIR(observed.st_mode):
        os.unlink(name, dir_fd=parent)

        return

    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)

    try:
        with os.scandir(child) as scratch_entries:
            for entry in scratch_entries:
                _remove_scratch(child, entry.name, deadline)
    finally:
        os.close(child)

    os.rmdir(name, dir_fd=parent)


def _operation(control, *, generation, lock_identity, lease, scratch_fd, argv, options, shutdown, status):
    operation = uuid.uuid4().hex
    worker = None
    science = child = None
    created = False
    greeting_delivered = False
    shutdown_requested = False

    try:
        started = time.monotonic()
        operation_deadline = None
        greeting_deadline = started + options.startup_seconds

        if options.operation_seconds is not None:
            operation_deadline = started + options.operation_seconds
            greeting_deadline = min(greeting_deadline, operation_deadline)

        os.mkdir(operation, mode=0o700, dir_fd=scratch_fd)
        created = True
        (science, child) = socket.socketpair()

        # Reference the supervisor's held root, without passing that root FD to
        # the child. It remains valid until this child's cleanup has settled.
        work = f"/proc/{os.getpid()}/fd/{scratch_fd}/{operation}"  # machine-string: subprocess cwd and TMPDIR use this exact held-root proc-fd path.

        environment = os.environ | {"TMPDIR": str(work)}

        worker = subprocess.Popen(
            (*argv, "--session-fd", str(child.fileno()), "--lease-fd", str(lease), "--operation", operation),
            pass_fds=(child.fileno(), lease),
            close_fds=True,
            cwd=work,
            env=environment,
            stdin=subprocess.DEVNULL,
        )

        child.close()

        LOGGER.info(
            "event=child_started operation=%s pid=%d",  # machine-string: operator event consumers parse these stable event and key=value fields.
            operation,
            worker.pid,
        )

        send_greeting(
            control,
            {
                "kind": "opened",
                "role": options.role,
                "generation": generation,
                "operation": operation,
                "lock": list(lock_identity)
            },
            science,
            deadline=greeting_deadline
        )

        greeting_delivered = True

        science.close()
        event = _watch(worker, control, shutdown, operation_deadline, status)
        shutdown_requested = event == "shutdown"
        returncode = _terminate(worker, options.terminate_seconds, options.kill_seconds)

        try:
            _remove_scratch(scratch_fd, operation, time.monotonic() + options.cleanup_seconds)
            created = False
        except (OSError, RecursionError) as cause:
            try:
                send_document(
                    control,
                    {
                        "kind": "retired",
                        "generation": generation,
                        "operation": operation,
                        "returncode": returncode,
                        "scratch_released": False,
                        "reason": event
                    },
                    deadline=time.monotonic() + options.startup_seconds
                )
            except OSError as delivery:
                LOGGER.warning(
                    "event=failed_cleanup_delivery operation=%s cause_type=%s errno=%s",  # machine-string: operator event consumers parse these stable event and key=value fields.
                    operation,
                    type(delivery).__name__,
                    delivery.errno
                )

            raise RetirementUnconfirmed("Native child stopped but scratch cleanup failed; runner admission must stop.") from cause

        control.settimeout(options.startup_seconds)

        try:
            send_document(
                control,
                {
                    "kind": "retired",
                    "generation": generation,
                    "operation": operation,
                    "returncode": returncode,
                    "scratch_released": True,
                    "reason": event
                },
                deadline=time.monotonic() + options.startup_seconds
            )
        except OSError as delivery:
            LOGGER.warning(
                "event=retirement_delivery_failed operation=%s child_reaped=true scratch_released=true cause_type=%s errno=%s",  # machine-string: operator event consumers parse these stable event and key=value fields.
                operation,
                type(delivery).__name__,
                delivery.errno
            )

        LOGGER.info(
            "event=child_retired operation=%s returncode=%d reason=%s",  # machine-string: operator event consumers parse these stable event and key=value fields.
            operation,
            returncode,
            event,
        )

        return shutdown_requested
    except RetirementUnconfirmed:
        # Reap/cleanup already spent its bound. Do not double it or erase the
        # distinct stopped-child/unreleased-scratch observation by retrying.
        raise
    except BaseException as primary:
        try:
            if worker is not None:
                _terminate(worker, options.terminate_seconds, options.kill_seconds)

            if created:
                _remove_scratch(scratch_fd, operation, time.monotonic() + options.cleanup_seconds)
        except BaseException as cleanup:
            raise BaseExceptionGroup("Runner setup and owned child/scratch cleanup both failed", [primary, cleanup]) from None

        if (worker is not None) and (not greeting_delivered) and (isinstance(primary, OSError)):
            # A queued caller can leave before its greeting. With its child
            # reaped and scratch removed, this does not poison the supervisor.
            LOGGER.warning(
                "event=opening_delivery_failed operation=%s child_reaped=true scratch_released=true cause_type=%s errno=%s",  # machine-string: operator event consumers parse these stable event and key=value fields.
                operation,
                type(primary).__name__,
                primary.errno
            )

            return False

        raise
    finally:
        if science is not None:
            science.close()

        if child is not None:
            child.close()


def serve(options, argv, shutdown):
    """Own the stable endpoint/lease until shutdown; accept only serial operations."""
    parent = _private_directory(options.socket.parent)
    scratch_fd = None
    status_fd = None
    lease = None
    identity = None

    try:
        scratch_fd = _private_directory(options.scratch)
        status_fd = _private_directory(options.status_directory)
        (lease, lock_identity) = _lease(parent)

        status_directory = pathlib.Path(
            f"/proc/self/fd/{status_fd}",  # machine-string: ProcessStatus reads through this exact inherited proc-fd directory.
        )

        status = ProcessStatus(status_directory, options.role)

        status.refresh("starting")

        scratch_entries = os.listdir(scratch_fd)

        if scratch_entries:
            raise RetirementUnconfirmed(f"{reprlib.repr(scratch_entries)=} should be empty before accepting a runner operation")

        if options.socket.name == LOCK_NAME:
            raise ValueError(f"{options.socket.name=} should differ from {LOCK_NAME=}")

        _unlink_socket(parent, options.socket.name)
        generation = uuid.uuid4().hex

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            previous = os.umask(0o177)

            try:
                listener.bind(
                    f"/proc/self/fd/{parent}/{options.socket.name}",  # machine-string: bind through this exact held-parent proc-fd path.
                )
            finally:
                os.umask(previous)

            observed = os.stat(options.socket.name, dir_fd=parent, follow_symlinks=False)
            identity = (observed.st_dev, observed.st_ino)

            listener.listen(1)

            with select.epoll() as events:
                events.register(listener.fileno(), select.EPOLLIN)
                events.register(shutdown, select.EPOLLIN)

                while True:
                    status.refresh("ready")
                    ready = dict(events.poll(5))

                    if shutdown in ready:
                        status.refresh("stopping")

                        return

                    if listener.fileno() not in ready:
                        continue

                    (control, _) = listener.accept()

                    with control:
                        status.refresh("working")

                        if _operation(
                            control,
                            generation=generation,
                            lock_identity=lock_identity,
                            lease=lease,
                            scratch_fd=scratch_fd,
                            argv=argv,
                            options=options,
                            shutdown=shutdown,
                            status=status
                        ):
                            status.refresh("stopping")

                            return
    finally:
        try:
            if identity is not None:
                _unlink_socket(parent, options.socket.name, identity)
        finally:
            if lease is not None:
                # Never LOCK_UN: a child may still hold this shared open-file description.
                os.close(lease)

            if scratch_fd is not None:
                os.close(scratch_fd)

            if status_fd is not None:
                os.close(status_fd)

            os.close(parent)


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Serve fixed native children on a private lifecycle socket.")

    parser.add_argument("--role", required=True)
    parser.add_argument("--socket", type=pathlib.Path, required=True)
    parser.add_argument("--scratch", type=pathlib.Path, required=True)
    parser.add_argument("--status-directory", type=pathlib.Path, required=True)
    lifetime = parser.add_mutually_exclusive_group(required=True)

    lifetime.add_argument("--operation-seconds", type=float)

    lifetime.add_argument(
        "--owner-session-lifetime",
        action="store_true",
        help="Use control-owner lifetime instead of an elapsed operation cap.",
    )

    for name in ("startup", "terminate", "kill", "cleanup"):
        parser.add_argument(
            f"--{name}-seconds",  # machine-string: argparse registers the literal option name; expression labels would change the accepted CLI.
            type=float,
            required=True,
        )

    parser.add_argument("worker", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)

    if options.operation_seconds is not None:
        require_positive_number(options.operation_seconds)

    for seconds in (options.startup_seconds, options.terminate_seconds, options.kill_seconds, options.cleanup_seconds):
        require_positive_number(seconds)

    if (not options.worker) or (options.worker[0] != "--") or (len(options.worker) < 2):
        parser.error("Supply one fixed worker command after --.")

    (read_fd, write_fd) = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    previous_wakeup = signal.set_wakeup_fd(write_fd)
    handlers = {}

    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            handlers[number] = signal.signal(number, lambda number, frame: None)

        serve(options, options.worker[1:], read_fd)
    finally:
        signal.set_wakeup_fd(previous_wakeup)

        for (number, handler) in handlers.items():
            signal.signal(number, handler)

        os.close(read_fd)
        os.close(write_fd)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
