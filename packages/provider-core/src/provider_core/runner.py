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
import select
import signal
import socket
import stat
import subprocess
import time
import uuid
from pathlib import Path

from provider_core.runner_protocol import RetirementUnconfirmed, positive_seconds, send_document, send_greeting
from provider_core.process_status import ProcessStatus

# Use a fixed diagnostic role for the reusable session owner.
# repo-check: allow-module-constant
LOGGER = logging.getLogger("provider_core.runner")
# One fixed lock leaf owns the exclusive runner session.
# repo-check: allow-module-constant
LOCK_NAME = ".owner-session.lock"


def _private_directory(path):
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Runner directories require canonical absolute paths without symlink ancestors.")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        observed = os.fstat(descriptor)
        if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o700:
            raise PermissionError("Runner directory must be caller-owned with mode 0700.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _lease(parent):
    descriptor = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o600:
            raise PermissionError("Runner lifetime lock must be a caller-owned mode 0600 regular file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor, (observed.st_dev, observed.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _unlink_socket(parent, name, identity=None):
    try:
        observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISSOCK(observed.st_mode) or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
        or identity is not None and (observed.st_dev, observed.st_ino) != identity
    ):
        raise PermissionError("Runner endpoint changed or is not a private owned socket; preserve it.")
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
                if deadline is not None and time.monotonic() >= deadline:
                    return "deadline"
    finally:
        os.close(pidfd)


def _remove_scratch(parent, name, deadline):
    """Delete only the reaped child's tree, checking the bound between entries."""
    if time.monotonic() >= deadline:
        raise TimeoutError("Runner scratch cleanup exceeded its declared deadline; preserve the tree.")
    observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(observed.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        with os.scandir(child) as entries:
            for entry in entries:
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
        science, child = socket.socketpair()
        # Reference the supervisor's held root, without passing that root FD to
        # the child. It remains valid until this child's cleanup has settled.
        work = f"/proc/{os.getpid()}/fd/{scratch_fd}/{operation}"
        environment = os.environ | {"TMPDIR": str(work)}
        worker = subprocess.Popen(
            (*argv, "--session-fd", str(child.fileno()), "--lease-fd", str(lease), "--operation", operation),
            pass_fds=(child.fileno(), lease), close_fds=True, cwd=work, env=environment,
            stdin=subprocess.DEVNULL,
        )
        child.close()
        LOGGER.info("event=child_started operation=%s pid=%d", operation, worker.pid)
        send_greeting(control, {"kind": "opened", "role": options.role, "generation": generation, "operation": operation,
                                "lock": list(lock_identity)}, science,
                      deadline=greeting_deadline)
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
                send_document(control, {"kind": "retired", "generation": generation, "operation": operation,
                                        "returncode": returncode, "scratch_released": False},
                              deadline=time.monotonic() + options.startup_seconds)
            except OSError as delivery:
                LOGGER.warning("event=failed_cleanup_delivery operation=%s cause_type=%s errno=%s",
                               operation, type(delivery).__name__, delivery.errno)
            raise RetirementUnconfirmed("Native child stopped but scratch cleanup failed; runner admission must stop.") from cause
        control.settimeout(options.startup_seconds)
        try:
            send_document(control, {"kind": "retired", "generation": generation, "operation": operation,
                                    "returncode": returncode, "scratch_released": True},
                          deadline=time.monotonic() + options.startup_seconds)
        except OSError as delivery:
            LOGGER.warning("event=retirement_delivery_failed operation=%s child_reaped=true scratch_released=true cause_type=%s errno=%s",
                           operation, type(delivery).__name__, delivery.errno)
        LOGGER.info("event=child_retired operation=%s returncode=%d reason=%s", operation, returncode, event)
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
        if worker is not None and not greeting_delivered and isinstance(primary, OSError):
            # A queued caller can leave before its greeting. With its child
            # reaped and scratch removed, this does not poison the supervisor.
            LOGGER.warning("event=opening_delivery_failed operation=%s child_reaped=true scratch_released=true cause_type=%s errno=%s",
                           operation, type(primary).__name__, primary.errno)
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
        lease, lock_identity = _lease(parent)
        status = ProcessStatus(Path(f"/proc/self/fd/{status_fd}"), options.role)
        status.refresh("starting")
        if os.listdir(scratch_fd):
            raise RetirementUnconfirmed("Runner scratch root is not empty; fresh-container cleanup is not established.")
        if options.socket.name == LOCK_NAME:
            raise ValueError("Runner endpoint cannot replace its lifetime lock.")
        _unlink_socket(parent, options.socket.name)
        generation = uuid.uuid4().hex
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            previous = os.umask(0o177)
            try:
                listener.bind(f"/proc/self/fd/{parent}/{options.socket.name}")
            finally:
                os.umask(previous)
            observed = os.stat(options.socket.name, dir_fd=parent, follow_symlinks=False)
            identity = observed.st_dev, observed.st_ino
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
                    control, _ = listener.accept()
                    with control:
                        status.refresh("working")
                        if _operation(control, generation=generation, lock_identity=lock_identity, lease=lease,
                                      scratch_fd=scratch_fd, argv=argv, options=options, shutdown=shutdown, status=status):
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
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--status-directory", type=Path, required=True)
    lifetime = parser.add_mutually_exclusive_group(required=True)
    lifetime.add_argument("--operation-seconds", type=float)
    lifetime.add_argument(
        "--owner-session-lifetime", action="store_true",
        help="Use control-owner lifetime instead of an elapsed operation cap.",
    )
    for name in ("startup", "terminate", "kill", "cleanup"):
        parser.add_argument(f"--{name}-seconds", type=float, required=True)
    parser.add_argument("worker", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)
    if options.operation_seconds is not None:
        positive_seconds(options.operation_seconds)
    for seconds in (options.startup_seconds, options.terminate_seconds, options.kill_seconds, options.cleanup_seconds):
        positive_seconds(seconds)
    if not options.worker or options.worker[0] != "--" or len(options.worker) < 2:
        parser.error("Supply one fixed worker command after --.")
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    previous_wakeup = signal.set_wakeup_fd(write_fd)
    handlers = {}
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            handlers[number] = signal.signal(number, lambda number, frame: None)
        serve(options, options.worker[1:], read_fd)
    finally:
        signal.set_wakeup_fd(previous_wakeup)
        for number, handler in handlers.items():
            signal.signal(number, handler)
        os.close(read_fd)
        os.close(write_fd)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
