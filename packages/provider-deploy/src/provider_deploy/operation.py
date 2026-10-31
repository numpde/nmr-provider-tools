"""One selected-engine identity operation, including exact owned retirement."""

from dataclasses import asdict, dataclass
import secrets
import threading
import time

from .docker import DockerEngine, DockerError, PROBE_SCRIPT
from .journal import Journal
from .linux import LinuxError, LinuxGuard
from .model import ProbeFailure, ProbeRequest, StepError


@dataclass(frozen=True)
class ProbeResult:
    engine: object
    host: object
    observation: object
    requested_uids: tuple[int, ...]
    requested_gids: tuple[int, ...]
    mapped_uids: tuple[tuple[int, int], ...]
    mapped_gids: tuple[tuple[int, int], ...]
    container_id: str
    operation_token: str
    retirement_confirmed: bool
    journal_path: str

    def as_dict(self) -> dict:
        return {"ok": True, **asdict(self)}


class _Budget:
    def __init__(self, seconds: float, command_seconds: float, cancel: threading.Event | None):
        self.deadline = time.monotonic() + seconds
        self.command_seconds = command_seconds
        self.cancel = cancel

    def remaining(self) -> float:
        if self.cancel is not None and self.cancel.is_set():
            raise StepError("cancelled", "operation was cancelled")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise StepError("timeout", "operation time bound expired")
        return left

    def arguments(self) -> dict:
        return {"timeout": min(self.command_seconds, self.remaining()), "cancel": self.cancel}


def _category(error: BaseException) -> str:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "cancelled"
    if isinstance(error, (DockerError, LinuxError, StepError)):
        return error.category
    if isinstance(error, OSError):
        return "local-io"
    return "internal"


def _owned(container, *, cid, token, name, image_id, request) -> None:
    expected_userns = "" if request.userns_mode == "default" else "host"
    expected_command = ("-c", PROBE_SCRIPT, token, str(request.bounds.lifetime_seconds))
    if (container.cid != cid or container.name != name or container.token != token
            or container.owner != "identity-v1" or container.image_id != image_id
            or container.user != f"{request.user_uid}:{request.user_gid}"
            or container.userns_mode != expected_userns
            or tuple(container.entrypoint) != ("/bin/sh",)
            or tuple(container.command) != expected_command):
        raise StepError("ownership", "probe ownership or configuration did not match")


def _live_identity(container) -> tuple:
    if not container.running or type(container.pid) is not int or container.pid <= 0:
        raise StepError("process-identity", "owned probe is not running")
    return (container.cid, container.pid, container.started_at)


def _retire(*, engine, guard, request, token, name, image_id, cid, observation, live_identity):
    """One removal at most; only exact verified ownership permits mutation."""
    budget = _Budget(request.bounds.cleanup_seconds, request.bounds.command_seconds, None)
    errors = []
    try:
        if cid is None:
            candidates = engine.candidates(token, **budget.arguments())
            if len(candidates) != 1:
                # Empty inventory cannot settle a creation request whose reply was lost.
                raise StepError("reconciliation", "creation outcome remains unresolved")
            cid = candidates[0]
        container = engine.inspect(cid, **budget.arguments())
        if container is not None:
            _owned(container, cid=cid, token=token, name=name, image_id=image_id, request=request)
            if observation is not None:
                try:
                    if live_identity is not None and _live_identity(container) != live_identity:
                        raise StepError("process-identity", "probe restarted or exited before retirement")
                    if guard.observe(container.pid, token) != observation:
                        raise StepError("process-identity", "probe maps changed before retirement")
                except (LinuxError, StepError, OSError) as error:
                    # The exact container is still ours to retire, but stale
                    # observation evidence must never be returned as success.
                    errors.append(_category(error))
            if observation is None and container.running and container.pid > 0:
                try:
                    observation = guard.observe(container.pid, token)
                except (LinuxError, OSError) as error:
                    errors.append(_category(error))
            try:
                engine.remove(cid, **budget.arguments())
            except BaseException as error:
                errors.append(_category(error))
                errors.extend(getattr(error, "cleanup_failures", ()))
            while True:
                container = engine.inspect(cid, **budget.arguments())
                if container is None:
                    break
                _owned(container, cid=cid, token=token, name=name, image_id=image_id, request=request)
                time.sleep(min(0.05, budget.remaining()))
        if observation is not None:
            while not guard.process_retired(observation):
                if budget.remaining() <= 0.05:
                    raise StepError("process-retirement", "probe process retirement remains unresolved")
                time.sleep(min(0.05, budget.remaining()))
        engine.assert_unchanged(**budget.arguments())
        return cid, "confirmed-retired", tuple(errors)
    except BaseException as error:
        errors.append(_category(error))
        errors.extend(getattr(error, "cleanup_failures", ()))
        return cid, "uncertain-outcome", tuple(errors)


def probe_identity(request: ProbeRequest, *, cancel: threading.Event | None = None) -> ProbeResult:
    """Map declared IDs on one local Docker engine, then retire before returning.

    The private journal is retained on success and failure. Its prepared record
    survives lost creation output and identifies the exact recovery candidates.
    Failed calls raise ProbeFailure containing no usable mappings. SIGINT-like
    cancellation enters an independent bounded cleanup window.
    """
    token = secrets.token_hex(16)
    name = "provider-deploy-" + token
    state = "known-not-created"
    cid = None
    engine = None
    journal = None
    observation = None
    live_identity = None
    image_id = None
    failure = None
    cleanup_failures = ()
    try:
        request.validate()
        budget = _Budget(request.bounds.operation_seconds, request.bounds.command_seconds, cancel)
        budget.remaining()
        guard = LinuxGuard()
        host = guard.host_evidence()
        budget.remaining()
        engine = DockerEngine.select(request.context, **budget.arguments())
        image_id = engine.image_id(request.image, **budget.arguments())
        try:
            journal = Journal(request.journal_path)
            journal.append({"event": "prepared", "version": 1, "state": state, "operation_token": token,
                "name": name, "engine": asdict(engine.identity), "image_id": image_id,
                "user_uid": request.user_uid, "user_gid": request.user_gid,
                "userns_mode": request.userns_mode, "lifetime_seconds": request.bounds.lifetime_seconds})
        except OSError as error:
            raise StepError("journal", "private durable recovery evidence could not be created") from error
        arguments = budget.arguments()
        # Set this BEFORE dispatch. A client failure need not mean Docker did not create.
        journal.append({"event": "create-intent", "state": "uncertain-outcome"})
        state = "uncertain-outcome"
        try:
            cid = engine.create(token=token, name=name, image_id=image_id, uid=request.user_uid,
                                gid=request.user_gid, userns_mode=request.userns_mode,
                                lifetime_seconds=request.bounds.lifetime_seconds, **arguments)
        except DockerError as error:
            if not error.mutation_uncertain:
                state = "known-not-created"
            raise
        state = "identified-resource"
        journal.append({"event": "identified", "state": state, "container_id": cid})
        container = engine.inspect(cid, **budget.arguments())
        if container is None:
            raise StepError("process-identity", "created probe disappeared before start")
        _owned(container, cid=cid, token=token, name=name, image_id=image_id, request=request)
        engine.start(cid, **budget.arguments())
        first = engine.inspect(cid, **budget.arguments())
        if first is None:
            raise StepError("process-identity", "started probe is absent")
        _owned(first, cid=cid, token=token, name=name, image_id=image_id, request=request)
        live_identity = _live_identity(first)
        observation = guard.observe(first.pid, token)
        budget.remaining()
        second = engine.inspect(cid, **budget.arguments())
        if second is None:
            raise StepError("process-identity", "probe disappeared during observation")
        _owned(second, cid=cid, token=token, name=name, image_id=image_id, request=request)
        if _live_identity(second) != live_identity or guard.observe(second.pid, token) != observation:
            raise StepError("process-identity", "probe PID, start time or maps changed during observation")
        if guard.host_evidence() != host:
            raise StepError("host-precondition", "reader namespace evidence changed")
        engine.assert_unchanged(**budget.arguments())
        mapped_uids = tuple((uid, observation.uid_map.translate(uid)) for uid in request.requested_uids)
        mapped_gids = tuple((gid, observation.gid_map.translate(gid)) for gid in request.requested_gids)
        budget.remaining()
    except BaseException as error:
        failure = error
    finally:
        if state != "known-not-created" and engine is not None:
            cid, state, cleanup_failures = _retire(engine=engine, guard=guard, request=request,
                token=token, name=name, image_id=image_id, cid=cid, observation=observation, live_identity=live_identity)
        if failure is not None:
            cleanup_failures = tuple(getattr(failure, "cleanup_failures", ())) + cleanup_failures
        if engine is not None:
            try:
                engine.close()
            except BaseException as error:
                cleanup_failures += ("client-retirement",)
                if failure is None:
                    failure = error
        # Cancellation never interrupts cleanup, but still invalidates delivery
        # of a success mapping when it arrives during retirement.
        if failure is None and cancel is not None and cancel.is_set():
            failure = StepError("cancelled", "operation was cancelled during retirement")
        if journal is not None:
            try:
                journal.append({"event": "finished", "state": state, "container_id": cid,
                    "failure": None if failure is None else _category(failure),
                    "cleanup_failures": list(cleanup_failures)})
            except BaseException as error:
                cleanup_failures += ("journal",)
                if failure is None:
                    failure = error
            finally:
                try:
                    journal.close()
                except OSError:
                    cleanup_failures += ("journal-close",)
    if failure is not None or cleanup_failures or state != "confirmed-retired":
        category = _category(failure) if failure is not None else "retirement"
        raise ProbeFailure(category=category, message=f"identity probe failed ({category}); no mapping was released",
            state=state, token=token, container_id=cid, journal_path=str(request.journal_path),
            cleanup_failures=cleanup_failures) from failure
    return ProbeResult(engine.identity, host, observation, request.requested_uids, request.requested_gids,
                       mapped_uids, mapped_gids, cid, token, True, str(request.journal_path))
