"""Bounded Docker CLI transport pinned to one explicitly selected local engine.

The probe verifies ownership before calling ``remove``. Engine output is parsed
locally; failures carry redacted diagnostic messages.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Mapping


from ._files import FileIdentity
from .spec import OWNER_LABEL, TOKEN_LABEL, ISOLATION_FIELDS
# Combined stdout/stderr limit for each client command.
# repo-check: allow-module-constant
OUTPUT_LIMIT = 65536
# Docker protocol admits only full container IDs.
# repo-check: allow-module-constant
CID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
# Owned label grammar accepts supported token wire lengths.
# repo-check: allow-module-constant
TOKEN_PATTERN = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{64})\Z")
# Resolved Docker image identity is a full sha256 digest.
# repo-check: allow-module-constant
IMAGE_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
# Fixed minimal Docker evidence projection; raw engine output never becomes diagnostics.
# repo-check: allow-module-constant
INFO_FORMAT = ('{"daemon_id":{{json .ID}},"security_options":{{json .SecurityOptions}},'
               '"root_dir":{{json .DockerRootDir}},"driver":{{json .Driver}},"os_type":{{json .OSType}}}')
# Fixed ownership/isolation evidence projection shared with ProbeSpec.verify.
# repo-check: allow-module-constant
INSPECT_FORMAT = (
    '{"cid":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
    '"token":{{json (index .Config.Labels "' + TOKEN_LABEL + '")}},'
    '"owner":{{json (index .Config.Labels "' + OWNER_LABEL + '")}},'
    '"pid":{{json .State.Pid}},"running":{{json .State.Running}},'
    '"started_at":{{json .State.StartedAt}},'
    '"user":{{json .Config.User}},"userns_mode":{{json .HostConfig.UsernsMode}},'
    '"entrypoint":{{json .Config.Entrypoint}},"command":{{json .Config.Cmd}},'
    '"mounts":{{json .Mounts}},"isolation":{' +
    ",".join('"' + field + '":{{json .HostConfig.' + field + '}}' for field in ISOLATION_FIELDS) + '}}'
)


class DockerError(Exception):
    """A stable, redacted failure; uncertain mutation must be reconciled."""

    # silent-defaults: allow-contract -- Dispatch marks uncertain mutations; failures before dispatch do not imply creation.
    def __init__(self, category: str, message: str, *, mutation_uncertain: bool = False):
        super().__init__(message)
        self.category = category
        self.mutation_uncertain = mutation_uncertain


def _command_failure(*errors: BaseException, mutation_uncertain: bool) -> BaseExceptionGroup:
    """Preserve ordered failures and dispatch uncertainty in an exception group."""
    failure = BaseExceptionGroup("Docker client failures.", errors)
    failure.mutation_uncertain = mutation_uncertain
    return failure


def _close_error(resource) -> BaseException | None:
    """Attempt one close and return its error, if any."""
    try:
        resource.close()
    except BaseException as error:
        return error
    return None


def _clean_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in source.items()
            if not key.startswith(("DOCKER_", "COMPOSE_", "BUILDX_"))
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}


def _deadline(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise DockerError("invalid_policy", "Command timeout must be finite and between zero and 3600 seconds.")
    return time.monotonic() + timeout


def _remaining(deadline: float) -> float:
    result = deadline - time.monotonic()
    if result <= 0:
        raise DockerError("timeout", "The bounded Docker operation timed out.")
    return result


def _peek_exit(process: subprocess.Popen):
    """Observe exit without freeing the PID that pins our private process group."""
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as error:
        raise DockerError("client_cleanup", "Docker child wait ownership was lost.") from error


def _reap(process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 1
    try:
        while True:
            pid, status = os.waitpid(process.pid, os.WNOHANG)
            if pid:
                process.returncode = os.waitstatus_to_exitcode(status)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DockerError("client_cleanup", "Docker client exit could not be confirmed.")
            time.sleep(min(0.01, remaining))
    except DockerError:
        raise
    except OSError as error:
        raise DockerError("client_cleanup", "Docker child could not be reaped.") from error
    except BaseException as error:
        raise _command_failure(error, DockerError("client_cleanup", "Docker child reaping was interrupted."),
                             mutation_uncertain=False) from error


def _terminate(process: subprocess.Popen) -> None:
    """Signal the owned group before reaping its leader, even after normal exit."""
    failure = None
    try:
        _peek_exit(process)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        # silent-defaults: allow-contract -- A vanished group needs no signal; its waitable leader must still be reaped.
        except ProcessLookupError:
            pass
    except DockerError:
        # Another waiter consumed the child: its numeric group may be reused.
        raise
    except OSError as error:
        failure = DockerError("client_cleanup", "Docker client group termination could not be confirmed.")
        failure.__cause__ = error
    except BaseException as error:
        failure = _command_failure(error, DockerError("client_cleanup", "Docker client retirement was interrupted."),
                                 mutation_uncertain=False)
    try:
        _reap(process)  # A failed signal must not skip reaping an already-exited child.
    except BaseException as error:
        if failure is not None:
            raise _command_failure(failure, error, mutation_uncertain=False) from failure
        raise
    if failure is not None:
        raise failure


# silent-defaults: allow-contract -- Read-only commands are the transport baseline; mutation callers explicitly opt into uncertain-dispatch reporting; tests/test_docker.py::CommandTests.
def _run_command(argv: list[str], *, env: Mapping[str, str], timeout: float,
                 cancel: threading.Event | None = None, mutation: bool = False) -> str:
    """Bound both output pipes; signal the private group and reap its leader.

    This operation exclusively owns waiting for its child: no SIGCHLD auto-reap
    or competing waiter. It confirms only the direct child's exit, not every
    descendant's exit. An interrupted spawn may return no child handle; its
    mutation remains uncertain. Client exit never establishes engine-side outcome.
    """
    deadline = _deadline(timeout)
    if cancel is not None and cancel.is_set():
        raise DockerError("cancelled", "Docker operation was cancelled before dispatch.")
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise DockerError("invalid_policy", "Docker client supervision requires waitable children.")
    # Allocate the polling resource before spawn: a selector allocation failure
    # must not strand a client that may already have submitted a mutation.
    try:
        selector = selectors.DefaultSelector()
    except OSError as exc:
        raise DockerError("client_unavailable", "Cannot prepare bounded Docker client supervision.") from exc
    output = bytearray()
    size = 0
    spawn_requested = False
    try:
        # Preparation may consume the budget or receive cancellation.
        if cancel is not None and cancel.is_set():
            raise DockerError("cancelled", "Docker operation was cancelled before dispatch.")
        _remaining(deadline)
        spawn_requested = True
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=dict(env), start_new_session=True)
    except BaseException as error:
        # Even OSError can come from a signal handler after Popen created a child.
        uncertain = mutation and spawn_requested
        operation = error
        if spawn_requested and isinstance(error, OSError):
            operation = DockerError("docker_unavailable", "Docker client startup did not complete.",
                                    mutation_uncertain=uncertain)
            operation.__cause__ = error
        cleanup = _close_error(selector)
        if cleanup is not None:
            raise _command_failure(operation, cleanup, mutation_uncertain=uncertain) from operation
        if spawn_requested and operation is error and isinstance(error, (DockerError, BaseExceptionGroup)):
            raise _command_failure(error, mutation_uncertain=uncertain) from error
        raise operation
    operation_error = retirement_error = None
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        events = ()
        while True:
            if cancel is not None and cancel.is_set():
                raise DockerError("cancelled", "Docker operation was cancelled.")
            for key, _ in events:
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                size += len(chunk)
                if size > OUTPUT_LIMIT:
                    raise DockerError("output_limit", "Docker command exceeded its output bound.")
                if key.fileobj is process.stdout:
                    output.extend(chunk)
            exited = _peek_exit(process)
            if exited is not None and not selector.get_map():
                break
            events = selector.select(min(0.05, _remaining(deadline)))
        if exited.si_code != os.CLD_EXITED or exited.si_status != 0:
            raise DockerError("docker_command", "Docker command did not report success.")
        try:
            decoded = output.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise DockerError("invalid_response", "Docker command returned non-UTF-8 output.") from exc
    except BaseException as error:
        operation_error = error
    finally:
        try:
            _terminate(process)
        except BaseException as error:
            retirement_error = error
        finally:
            # Evaluate every close eagerly, even when an earlier one fails.
            close_errors = [_close_error(selector), _close_error(process.stdout), _close_error(process.stderr)]
    errors = [error for error in (operation_error, retirement_error, *close_errors) if error is not None]
    if errors:
        first = errors[0]
        if len(errors) > 1 or isinstance(first, BaseExceptionGroup):
            raise _command_failure(*errors, mutation_uncertain=mutation) from first
        if isinstance(first, DockerError):
            first.mutation_uncertain = mutation
        raise first
    return decoded


def _json(text: str):
    def object_pairs(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("Duplicate structured field")
        return value
    try:
        return json.loads(text, object_pairs_hook=object_pairs)
    except (ValueError, TypeError, RecursionError) as exc:
        raise DockerError("invalid_response", "Docker returned an invalid structured response.") from exc


# silent-defaults: allow-contract -- Docker evidence text has a fixed 4096-character protocol bound; callers can supply a narrower bound.
def _text(value, maximum=4096) -> bool:
    return isinstance(value, str) and len(value) <= maximum and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _cid(value: str) -> None:
    if not isinstance(value, str) or CID_PATTERN.fullmatch(value) is None:
        raise DockerError("invalid_identity", "An exact full Docker container ID is required.")


def _token(value: str) -> None:
    if not isinstance(value, str) or TOKEN_PATTERN.fullmatch(value) is None:
        raise DockerError("invalid_identity", "A random 128-bit or 256-bit hexadecimal operation token is required.")


def _socket_identity(path: str) -> FileIdentity:
    try:
        info = os.stat(path)
    except OSError as exc:
        raise DockerError("engine_changed", "The selected local Docker socket is unavailable.") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise DockerError("unsupported_engine", "The selected Docker endpoint is not a Unix socket.")
    return FileIdentity.of(info)


@dataclass(frozen=True)
class EngineInfo:
    """The daemon settings that must stay fixed throughout a probe operation."""

    daemon_id: str
    security_options: tuple[str, ...]
    root_dir: str
    driver: str
    os_type: str

    @classmethod
    def of(cls, text: str) -> EngineInfo:
        value = _json(text)
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise DockerError("invalid_response", "Docker returned an invalid engine identity.")
        if any(not _text(value[key]) or not value[key]
               for key in ("daemon_id", "root_dir", "driver", "os_type")):
            raise DockerError("invalid_response", "Docker returned an invalid engine identity.")
        # silent-defaults: allow-contract -- Moby emits null for a nil SecurityOptions slice; null and [] both mean no entries. See test_docker.py::EngineTests.test_null_and_empty_security_options_are_equivalent_but_change_is_not.
        options = [] if value["security_options"] is None else value["security_options"]
        if (not isinstance(options, list) or len(options) > 32
                or not all(_text(option, 512) for option in options)):
            raise DockerError("invalid_response", "Docker returned an invalid engine identity.")
        if value["os_type"] != "linux":
            raise DockerError("unsupported_engine", "Only local Linux Docker engines are supported.")
        value["security_options"] = tuple(sorted(options))
        return cls(**value)


@dataclass(frozen=True)
class EngineIdentity:
    endpoint: str
    socket: FileIdentity
    info: EngineInfo


@dataclass(frozen=True)
class Container:
    cid: str
    name: str
    image_id: str
    token: str | None
    owner: str | None
    pid: int
    running: bool
    started_at: str
    user: str
    userns_mode: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    mounts: list
    isolation: dict

    @classmethod
    def from_json(cls, text: str, cid: str) -> Container:
        """Parse the complete projected record before it can serve as evidence."""
        value = _json(text)
        keys = {field.name for field in fields(cls)}
        if not isinstance(value, dict) or set(value) != keys:
            raise DockerError("invalid_response", "Docker returned invalid container evidence.")
        if (not isinstance(value["mounts"], list) or not isinstance(value["isolation"], dict)
                or set(value["isolation"]) != set(ISOLATION_FIELDS)):
            raise DockerError("invalid_response", "Docker returned invalid isolation evidence.")
        text_fields = ("cid", "name", "image_id", "started_at", "user", "userns_mode")
        if any(not _text(value[key]) for key in text_fields):
            raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        if any(value[key] is not None and not _text(value[key]) for key in ("token", "owner")):
            raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        if type(value["pid"]) is not int or value["pid"] < 0 or type(value["running"]) is not bool:
            raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        for field in ("entrypoint", "command"):
            arguments = value[field]
            if (not isinstance(arguments, list) or len(arguments) > 16
                    or any(not _text(item) for item in arguments)):
                raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        if value["cid"] != cid:
            raise DockerError("identity_mismatch", "Docker inspection returned another container.")
        value["name"] = value["name"].removeprefix("/")
        value["entrypoint"] = tuple(value["entrypoint"])
        value["command"] = tuple(value["command"])
        return cls(**value)


class DockerEngine:
    """Selected socket and daemon identity retained for one complete operation."""

    def __init__(self, identity: EngineIdentity, directory: tempfile.TemporaryDirectory,
                 executable: str, environment: Mapping[str, str]):
        self.identity = identity
        self._directory = directory
        self._executable = executable
        self._environment = dict(environment)
        self._closed = False

    @classmethod
    # silent-defaults: allow-contract -- Use the Docker CLI transport unless the caller supplies a test executable; never choose a Docker endpoint implicitly; tests/test_docker.py::EngineTests.
    def connect(cls, endpoint: str, *, timeout: float, cancel: threading.Event | None = None,
                executable: str = "docker") -> DockerEngine:
        """Connect to the selected Unix socket and record its daemon identity."""
        deadline = _deadline(timeout)
        if not _text(endpoint) or not endpoint.startswith("unix:///") or "?" in endpoint or "#" in endpoint:
            raise DockerError("unsupported_engine", "The selected Docker endpoint must be a local Unix socket.")
        # Docker trims its host option; do not stat one path and dial another.
        if endpoint != endpoint.strip():
            raise DockerError("unsupported_engine", "The selected Docker endpoint must not have surrounding whitespace.")
        # Keep the caller's absolute path, including symlinks: subsequent
        # caller commands use this spelling, so every guard must follow it.
        socket_path = endpoint[7:]
        socket = _socket_identity(socket_path)
        environment = _clean_environment(os.environ)
        directory = tempfile.TemporaryDirectory(prefix="linux-idmap-client-")
        try:
            argv = [executable, "--config", directory.name, "--host", endpoint, "info", "--format", INFO_FORMAT]
            info = EngineInfo.of(_run_command(argv, env=environment, timeout=_remaining(deadline), cancel=cancel))
            if socket != _socket_identity(socket_path):
                raise DockerError("engine_changed", "The selected Docker socket changed while resolving its identity.")
            identity = EngineIdentity(endpoint, socket, info)
            return cls(identity, directory, executable, environment)
        except BaseException as operation:
            try:
                directory.cleanup()
            except BaseException as cleanup:
                raise _command_failure(operation, cleanup, mutation_uncertain=False) from operation
            raise

    def __enter__(self) -> DockerEngine:
        return self

    def __exit__(self, _kind, operation, _traceback) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if operation is not None:
                raise _command_failure(operation, cleanup, mutation_uncertain=True) from operation
            raise

    def close(self) -> None:
        self._directory.cleanup()
        self._closed = True

    def _argv(self, args: list[str]) -> list[str]:
        if self._closed:
            raise DockerError("invalid_policy", "This Docker engine operation has already closed.")
        return [self._executable, "--config", self._directory.name, "--host", self.identity.endpoint, *args]

    def _socket_check(self) -> None:
        if _socket_identity(self.identity.endpoint[7:]) != self.identity.socket:
            raise DockerError("engine_changed", "The selected Docker socket identity changed.")

    def assert_unchanged(self, *, timeout: float, cancel: threading.Event | None = None) -> None:
        deadline = _deadline(timeout)
        self._socket_check()
        value = EngineInfo.of(_run_command(self._argv(["info", "--format", INFO_FORMAT]),
                                         env=self._environment, timeout=_remaining(deadline), cancel=cancel))
        if value != self.identity.info:
            raise DockerError("engine_changed", "The selected Docker daemon identity or namespace configuration changed.")
        self._socket_check()

    # silent-defaults: allow-contract -- Transport queries are nonmutating; create/start/remove explicitly identify mutation uncertainty; tests/test_docker.py::CommandTests.
    def _call(self, args: list[str], *, timeout: float, cancel: threading.Event | None = None,
              mutation: bool = False) -> str:
        deadline = _deadline(timeout)
        self.assert_unchanged(timeout=_remaining(deadline), cancel=cancel)
        completed = False
        try:
            result = _run_command(self._argv(args), env=self._environment, timeout=_remaining(deadline),
                                  cancel=cancel, mutation=mutation)
            completed = True
            self.assert_unchanged(timeout=_remaining(deadline), cancel=cancel)
            return result
        except (DockerError, BaseExceptionGroup) as exc:
            if completed and mutation:
                if isinstance(exc, BaseExceptionGroup):
                    raise _command_failure(exc, mutation_uncertain=True) from exc
                exc.mutation_uncertain = True
            raise

    def image_id(self, image: str, *, timeout: float, cancel: threading.Event | None = None) -> str:
        if not _text(image, 512) or not image or image.startswith("-"):
            raise DockerError("invalid_policy", "An explicit approved local image reference is required.")
        value = _json(self._call(["image", "inspect", "--format", "[{{json .ID}},{{json .Config.Volumes}}]", image], timeout=timeout, cancel=cancel))
        if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str) or IMAGE_PATTERN.fullmatch(value[0]) is None:
            raise DockerError("invalid_response", "The approved local image did not resolve to an immutable image ID.")
        if value[1] not in (None, {}):
            raise DockerError("invalid_policy", "The approved probe image must not declare implicit volumes.")
        return value[0]

    def create(self, arguments: list[str], *, timeout: float,
               cancel: threading.Event | None = None) -> str:
        value = self._call(arguments, timeout=timeout, cancel=cancel, mutation=True).strip()
        try:
            _cid(value)
        except DockerError as exc:
            exc.mutation_uncertain = True
            raise
        return value

    def start(self, cid: str, *, timeout: float, cancel: threading.Event | None = None) -> None:
        _cid(cid)
        self._call(["container", "start", cid], timeout=timeout, cancel=cancel, mutation=True)

    def _list(self, filter_value: str, *, timeout: float, cancel: threading.Event | None = None) -> tuple[str, ...]:
        result = self._call(["container", "ls", "--all", "--no-trunc", "--quiet", "--filter", filter_value],
                            timeout=timeout, cancel=cancel)
        values = result.splitlines()
        if len(values) > 64 or len(set(values)) != len(values):
            raise DockerError("invalid_response", "Docker candidate listing exceeded the identity bound.")
        for value in values:
            _cid(value)
        return tuple(values)

    def candidates(self, token: str, *, timeout: float, cancel: threading.Event | None = None) -> tuple[str, ...]:
        _token(token)
        return self._list("label=" + TOKEN_LABEL + "=" + token, timeout=timeout, cancel=cancel)

    def inspect(self, cid: str, *, timeout: float, cancel: threading.Event | None = None) -> Container | None:
        _cid(cid)
        deadline = _deadline(timeout)
        try:
            result = self._call(["container", "inspect", "--format", INSPECT_FORMAT, cid],
                                timeout=_remaining(deadline), cancel=cancel)
        except DockerError as error:
            if error.category != "docker_command":
                raise
            # A nonzero inspect is not proof of absence. Confirm it by a
            # successful exact-ID inventory, without parsing localized stderr.
            # Existing containers need only the inspect and its engine guards.
            values = self._list("id=" + cid, timeout=_remaining(deadline), cancel=cancel)
            if not values:
                return None
            if values != (cid,):
                raise DockerError("identity_mismatch", "Docker exact-ID listing returned another container.") from error
            raise
        return Container.from_json(result, cid)

    def remove(self, cid: str, *, timeout: float, cancel: threading.Event | None = None) -> None:
        _cid(cid)
        # The owning probe verifies this exact container ID before removal.
        self._call(["container", "rm", "--force", cid], timeout=timeout, cancel=cancel, mutation=True)
