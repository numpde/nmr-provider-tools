"""Bounded Docker CLI transport pinned to one explicitly selected local engine.

This module never pulls an image and does not itself grant ownership to callers.
The complete identity operation verifies ownership before calling ``remove``.
Engine command output is parsed locally and never used as a diagnostic message.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
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


from .spec import OWNER_LABEL, TOKEN_LABEL, ISOLATION_FIELDS, ProbeSpec
# Transport memory bound, not an engine-selected setting; output-limit tests exercise refusal.
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
INFO_FORMAT = ('[{{json .ID}},{{json .SecurityOptions}},{{json .DockerRootDir}},'
               '{{json .Driver}},{{json .OSType}}]')
# Fixed ownership/isolation evidence projection shared with ProbeSpec.verify.
# repo-check: allow-module-constant
INSPECT_FORMAT = (
    '{"cid":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
    '"token":{{json (index .Config.Labels "' + TOKEN_LABEL + '")}},'
    '"owner":{{json (index .Config.Labels "' + OWNER_LABEL + '")}},'
    '"pid":{{json .State.Pid}},"running":{{json .State.Running}},'
    '"status":{{json .State.Status}},"started_at":{{json .State.StartedAt}},'
    '"user":{{json .Config.User}},"userns_mode":{{json .HostConfig.UsernsMode}},'
    '"entrypoint":{{json .Config.Entrypoint}},"command":{{json .Config.Cmd}},'
    '"mounts":{{json .Mounts}},"isolation":{' +
    ",".join('"' + field + '":{{json .HostConfig.' + field + '}}' for field in ISOLATION_FIELDS) + '}}'
)


class DockerError(Exception):
    """A stable, redacted failure; uncertain mutation must be reconciled."""

    # silent-defaults: allow-contract -- Error objects describe nonmutating failures unless dispatch records uncertainty; cleanup_failures is an immutable empty diagnostic inventory; tests/test_docker.py::CommandTests.
    def __init__(self, category: str, message: str, *, mutation_uncertain: bool = False,
                 cleanup_failures: tuple[str, ...] = ()):
        super().__init__(message)
        self.category = category
        self.mutation_uncertain = mutation_uncertain
        self.cleanup_failures = cleanup_failures


def _clean_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in source.items()
            if not key.startswith(("DOCKER_", "COMPOSE_", "BUILDX_"))
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}


def _deadline(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise DockerError("invalid_policy", "Command timeout must be finite and between zero and 3600 seconds.")
    return time.monotonic() + timeout


def _remaining(deadline: float) -> float:
    result = deadline - time.monotonic()
    if result <= 0:
        raise DockerError("timeout", "The bounded Docker operation timed out.")
    return result


def _terminate(process: subprocess.Popen) -> None:
    # A private process group also contains any child spawned by a CLI wrapper.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    # silent-defaults: allow-contract -- An already-exited process group needs no kill; wait still confirms the direct child below; tests/test_docker.py::CommandTests.
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise DockerError("client_cleanup", "Docker client termination could not be confirmed.", mutation_uncertain=True) from exc
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired as exc:
        raise DockerError("client_cleanup", "Docker client exit could not be confirmed.", mutation_uncertain=True) from exc


# silent-defaults: allow-contract -- Read-only commands are the transport baseline; mutation callers explicitly opt into uncertain-dispatch reporting; tests/test_docker.py::CommandTests.
def _run_command(argv: list[str], *, env: Mapping[str, str], timeout: float,
                 cancel: threading.Event | None = None, mutation: bool = False) -> str:
    """Stream at most OUTPUT_LIMIT bytes across both pipes; kill/reap on failure.

    A client exit/kill is never proof that an engine-side mutation did not happen.
    The caller owns reconciliation from its retained operation token.
    """
    deadline = _deadline(timeout)
    if cancel is not None and cancel.is_set():
        raise DockerError("cancelled", "Docker operation was cancelled before dispatch.")
    # Allocate the polling resource before spawn: a selector allocation failure
    # must not strand a client that may already have submitted a mutation.
    try:
        selector = selectors.DefaultSelector()
    except OSError as exc:
        raise DockerError("client_unavailable", "Cannot prepare bounded Docker client supervision.") from exc
    output = bytearray()
    size = 0
    reaped = False
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=dict(env), start_new_session=True)
    except BaseException as exc:
        selector.close()
        if isinstance(exc, OSError):
            raise DockerError("docker_unavailable", "Cannot execute the Docker client.") from exc
        raise
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while True:
            if cancel is not None and cancel.is_set():
                raise DockerError("cancelled", "Docker operation was cancelled.")
            if time.monotonic() >= deadline:
                raise DockerError("timeout", "The bounded Docker command timed out.")
            wait = min(0.05, _remaining(deadline))
            if not selector.get_map():
                status = process.poll()
                if status is not None:
                    reaped = True
                    break
            for key, _ in selector.select(wait):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                size += len(chunk)
                if size > OUTPUT_LIMIT:
                    raise DockerError("output_limit", "Docker command exceeded its output bound.")
                if key.fileobj is process.stdout:
                    output.extend(chunk)
        if status != 0:
            raise DockerError("docker_command", "Docker command did not report success.")
        try:
            return output.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise DockerError("invalid_response", "Docker command returned non-UTF-8 output.") from exc
    except BaseException as exc:
        if not reaped:
            try:
                _terminate(process)
            except DockerError as cleanup:
                exc.add_note(str(cleanup))
                exc.cleanup_failures = (*getattr(exc, "cleanup_failures", ()), cleanup.category)
        if isinstance(exc, DockerError) and mutation:
            exc.mutation_uncertain = True
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _json(text: str):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate structured field")
            value[key] = item
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


def _socket_identity(path: str) -> tuple[int, int]:
    try:
        info = os.stat(path)
    except OSError as exc:
        raise DockerError("engine_changed", "The selected local Docker socket is unavailable.") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise DockerError("unsupported_engine", "The selected Docker endpoint is not a Unix socket.")
    return info.st_dev, info.st_ino


def _fingerprint(text: str) -> tuple[str, tuple[str, ...], str, str, str]:
    values = _json(text)
    if (not isinstance(values, list) or len(values) != 5 or
        not all(_text(values[i]) and values[i] for i in (0, 2, 3, 4))):
        raise DockerError("invalid_response", "Docker returned an invalid engine identity.")
    # silent-defaults: allow-contract -- Moby emits a nil SecurityOptions slice when no features append; null and [] both encode no entries, not missing policy; tests/test_docker.py::EngineTests.test_null_and_empty_security_options_are_equivalent_but_change_is_not.
    security_options = [] if values[1] is None else values[1]
    if (not isinstance(security_options, list) or len(security_options) > 32 or
            not all(_text(x, 512) for x in security_options)):
        raise DockerError("invalid_response", "Docker returned an invalid engine identity.")
    if values[4] != "linux":
        raise DockerError("unsupported_engine", "Only local Linux Docker engines are supported.")
    return values[0], tuple(sorted(security_options)), values[2], values[3], values[4]


@dataclass(frozen=True)
class EngineIdentity:
    endpoint: str
    daemon_id: str
    socket_device: int
    socket_inode: int
    security_options: tuple[str, ...]
    docker_root_dir: str
    storage_driver: str
    os_type: str


@dataclass(frozen=True)
class Container:
    cid: str
    name: str
    image_id: str
    token: str | None
    owner: str | None
    pid: int
    running: bool
    status: str
    started_at: str
    user: str
    userns_mode: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    mounts: list
    isolation: dict


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
        """Use the caller's frozen endpoint; never consult an ambient context."""
        deadline = _deadline(timeout)
        if not _text(endpoint) or not endpoint.startswith("unix:///") or "?" in endpoint or "#" in endpoint:
            raise DockerError("unsupported_engine", "The selected Docker endpoint must be a local Unix socket.")
        # Keep the caller's absolute path, including symlinks: subsequent
        # caller commands use this spelling, so every guard must follow it.
        socket_path = endpoint[7:]
        device, inode = _socket_identity(socket_path)
        environment = _clean_environment(os.environ)
        directory = tempfile.TemporaryDirectory(prefix="provider-deploy-client-")
        try:
            argv = [executable, "--config", directory.name, "--host", endpoint, "info", "--format", INFO_FORMAT]
            fingerprint = _fingerprint(_run_command(argv, env=environment, timeout=_remaining(deadline), cancel=cancel))
            if (device, inode) != _socket_identity(socket_path):
                raise DockerError("engine_changed", "The selected Docker socket changed while resolving its identity.")
            daemon_id, security_options, root_dir, driver, os_type = fingerprint
            identity = EngineIdentity(endpoint, daemon_id, device, inode, security_options, root_dir, driver, os_type)
            return cls(identity, directory, executable, environment)
        except BaseException:
            directory.cleanup()
            raise

    def __enter__(self) -> DockerEngine:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self._directory.cleanup()
        self._closed = True

    def _argv(self, args: list[str]) -> list[str]:
        if self._closed:
            raise DockerError("invalid_policy", "This Docker engine operation has already closed.")
        return [self._executable, "--config", self._directory.name, "--host", self.identity.endpoint, *args]

    def _socket_check(self) -> None:
        if _socket_identity(self.identity.endpoint[7:]) != (self.identity.socket_device, self.identity.socket_inode):
            raise DockerError("engine_changed", "The selected Docker socket identity changed.")

    def assert_unchanged(self, *, timeout: float, cancel: threading.Event | None = None) -> None:
        deadline = _deadline(timeout)
        self._socket_check()
        value = _fingerprint(_run_command(self._argv(["info", "--format", INFO_FORMAT]),
                                         env=self._environment, timeout=_remaining(deadline), cancel=cancel))
        expected = (self.identity.daemon_id, self.identity.security_options, self.identity.docker_root_dir,
                    self.identity.storage_driver, self.identity.os_type)
        if value != expected:
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
        except DockerError as exc:
            if completed and mutation:
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

    def create(self, spec: ProbeSpec, *, timeout: float,
               cancel: threading.Event | None = None) -> str:
        value = self._call(spec.create_arguments(), timeout=timeout, cancel=cancel, mutation=True).strip()
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
        value = _json(result)
        keys = {"cid", "name", "image_id", "token", "owner", "pid", "running", "status", "started_at",
                "user", "userns_mode", "entrypoint", "command", "mounts", "isolation"}
        if not isinstance(value, dict) or set(value) != keys:
            raise DockerError("invalid_response", "Docker returned invalid container evidence.")
        if (not isinstance(value["mounts"], list) or not isinstance(value["isolation"], dict)
                or set(value["isolation"]) != set(ISOLATION_FIELDS)):
            raise DockerError("invalid_response", "Docker returned invalid isolation evidence.")
        if (any(not _text(value[key]) for key in ("cid", "name", "image_id", "status", "started_at", "user", "userns_mode")) or
            any(value[key] is not None and not _text(value[key]) for key in ("token", "owner")) or
            type(value["pid"]) is not int or value["pid"] < 0 or type(value["running"]) is not bool or
            any(not isinstance(value[key], list) or len(value[key]) > 16 or not all(_text(item) for item in value[key])
                for key in ("entrypoint", "command"))):
            raise DockerError("invalid_response", "Docker returned invalid container evidence fields.")
        if value["cid"] != cid:
            raise DockerError("identity_mismatch", "Docker inspection returned another container.")
        return Container(**{**value, "name": value["name"].removeprefix("/"),
                            "entrypoint": tuple(value["entrypoint"]), "command": tuple(value["command"])})

    def remove(self, cid: str, *, timeout: float, cancel: threading.Event | None = None) -> None:
        _cid(cid)
        # This is deliberately private-operation scope: exact ownership is checked
        # by the owning probe immediately before removal. Never remove by name or token.
        self._call(["container", "rm", "--force", cid], timeout=timeout, cancel=cancel, mutation=True)
