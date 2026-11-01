"""Measure fresh maps, retire the owned probe, then release the result."""

from dataclasses import asdict, dataclass
import secrets
import threading
import time

from .docker import Container, DockerEngine, DockerError
from .linux import IdMap, LinuxError, LinuxGuard, ProcessIdentity, ProcessObservation
from .model import Problem, ProbeFailure, ProbeHandle, ProbeRequest, ResourceState, Retirement, StepError
from .spec import ProbeSpec


@dataclass(frozen=True)
class ProbeResult:
    uid_map: IdMap
    gid_map: IdMap
    container_id: str

    def as_dict(self) -> dict:
        return {"ok": True, "uid_map": asdict(self.uid_map)["rows"],
                "gid_map": asdict(self.gid_map)["rows"], "container_id": self.container_id,
                "state": ResourceState.RETIRED}


class _Budget:
    def __init__(self, seconds: float, command_seconds: float, cancel=None):
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

    def pause(self):
        time.sleep(min(0.05, self.remaining()))


def _problem(stage: str, error: BaseException) -> Problem:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return Problem(stage, "cancelled", "operation was interrupted")
    if isinstance(error, (DockerError, LinuxError, StepError)):
        return Problem(stage, error.category, str(error))
    if isinstance(error, OSError):
        return Problem(stage, "local-io", "local operation failed; inspect the probe handle")
    return Problem(stage, "internal", "unexpected operation failure; inspect the probe handle")


def _client_problems(error: BaseException) -> tuple[Problem, ...]:
    # repo-check: allow-tuple-generator -- Retirement owns an immutable diagnostic snapshot, never a live view of client failures.
    return tuple(Problem("client-retirement", category, "Docker client retirement was not confirmed")
                 for category in getattr(error, "cleanup_failures", ()))


def _running_identity(container: Container) -> tuple[str, int, str]:
    if not container.running or type(container.pid) is not int or container.pid <= 0:
        raise StepError("process-identity", "owned probe is not running")
    return container.cid, container.pid, container.started_at


class _Probe:
    """The sole owner of an operation's resource and its retirement protocol."""

    def __init__(self, engine: DockerEngine, guard: LinuxGuard, spec: ProbeSpec):
        self.engine = engine
        self.guard = guard
        self.spec = spec
        self.may_exist = False
        self.stage = "create"
        self.cid: str | None = None
        self.observation: ProcessObservation | None = None
        self.running_identity: tuple[str, int, str] | None = None
        self.processes: list[ProcessIdentity] = []
        self.unobserved_pids: list[int] = []
        self.retirement_cause: BaseException | None = None

    def handle(self) -> ProbeHandle:
        return ProbeHandle(
            endpoint=self.engine.identity.endpoint,
            operation_token=self.spec.token,
            container_id=self.cid,
            processes=tuple(self.processes),
            unobserved_pids=tuple(self.unobserved_pids),
            daemon_id=self.engine.identity.daemon_id,
        )

    def track(self, process: ProcessIdentity):
        identity = ProcessIdentity(process.pid, process.starttime, process.proc_device, process.proc_inode)
        if identity not in self.processes:
            self.processes.append(identity)

    def inspect(self, budget: _Budget) -> Container | None:
        container = self.engine.inspect(self.cid, **budget.arguments())
        if container is not None:
            self.spec.verify(container, self.cid)
        return container

    def measure(self, budget: _Budget):
        arguments = budget.arguments()
        self.may_exist = True  # client failure cannot prove non-creation
        try:
            self.cid = self.engine.create(self.spec, **arguments)
        except DockerError as error:
            if not error.mutation_uncertain:
                self.may_exist = False
            raise
        self.stage = "start"
        if self.inspect(budget) is None:
            raise StepError("process-identity", "created probe disappeared before start")
        self.engine.start(self.cid, **budget.arguments())
        self.stage = "observe"
        first = self.inspect(budget)
        if first is None:
            raise StepError("process-identity", "started probe is absent")
        self.running_identity = _running_identity(first)
        self.observation = self.guard.observe(first.pid, self.spec.token)
        self.track(self.observation)
        if not self.observation_matches(self.inspect(budget)):
            raise StepError("process-identity", "probe process or maps changed during observation")
        budget.remaining()

    def observation_matches(self, container: Container | None) -> bool:
        return (container is not None
                and _running_identity(container) == self.running_identity
                and self.guard.observe(container.pid, self.spec.token) == self.observation)

    def retire(self) -> tuple[Retirement, Problem | None]:
        """Run once after success or failure. Never creates or returns maps."""
        if not self.may_exist:
            return Retirement(ResourceState.NOT_CREATED), None
        state = ResourceState.UNRESOLVED
        problems = []
        invalid_observation = None
        try:
            bounds = self.spec.request.bounds
            budget = _Budget(bounds.cleanup_seconds, bounds.command_seconds)
            if self.cid is None:
                candidates = self.engine.candidates(self.spec.token, **budget.arguments())
                if len(candidates) != 1:
                    raise StepError("reconciliation", "creation remains unresolved; no unique owned candidate")
                self.cid = candidates[0]
            container = self.inspect(budget)
            if self.observation is not None:
                try:
                    if not self.observation_matches(container):
                        raise StepError("process-identity", "probe process or maps changed before retirement")
                except (LinuxError, StepError, OSError) as error:
                    self.retirement_cause = error
                    invalid_observation = _problem("observe", error)
            # A reported PID remains a cleanup obligation even when the engine
            # says it is stopped. A lost start reply or restart needs fresh identity.
            if (container is not None and (container.running or container.pid > 0)
                    and (self.observation is None or invalid_observation is not None)):
                try:
                    current = self.guard.identify(container.pid, self.spec.token)
                except (LinuxError, OSError) as error:
                    if self.retirement_cause is None:
                        self.retirement_cause = error
                    problems.append(_problem("observe", error))
                    self.unobserved_pids.append(container.pid)
                else:
                    self.track(current)
            if container is not None:
                try:
                    self.engine.remove(container.cid, **budget.arguments())
                except BaseException as error:
                    if self.retirement_cause is None:
                        self.retirement_cause = error
                    problems.append(_problem("remove", error))
                    problems.extend(_client_problems(error))
                while self.inspect(budget) is not None:
                    budget.pause()
            for process in self.processes:
                while not self.guard.process_retired(process):
                    budget.pause()
            for pid in self.unobserved_pids:
                while not self.guard.pid_absent(pid):
                    budget.pause()
            self.engine.assert_unchanged(**budget.arguments())
            state = ResourceState.RETIRED
        except BaseException as error:
            if self.retirement_cause is None:
                self.retirement_cause = error
            problems.append(_problem("retire", error))
            problems.extend(_client_problems(error))
        return Retirement(state, self.cid, tuple(problems)), invalid_observation


# silent-defaults: allow-contract -- The public operation uses the Docker CLI transport; an explicit executable override supports fault qualification without changing endpoint policy; tests/test_operation_faults.py::OperationFaultTests.
def probe_identity(request: ProbeRequest, *, cancel: threading.Event | None = None,
                   executable: str = "docker") -> ProbeResult:
    """Return validated full maps only after exact probe retirement."""
    problems = []
    cause = None
    engine = None
    retirement = Retirement(ResourceState.NOT_CREATED)
    token = secrets.token_hex(16)
    stage = "policy"
    probe = None
    try:
        request.validate()
        budget = _Budget(request.bounds.operation_seconds, request.bounds.command_seconds, cancel)
        budget.remaining()
        stage = "host"
        guard = LinuxGuard()
        host = guard.host_evidence()
        stage = "engine"
        engine = DockerEngine.connect(request.endpoint, executable=executable, **budget.arguments())
        image_id = engine.image_id(request.image, **budget.arguments())
        spec = ProbeSpec(token, image_id, request)
        probe = _Probe(engine, guard, spec)
        # Until retirement returns a proof, interruption must not imply absence.
        retirement = Retirement(ResourceState.UNRESOLVED)
        try:
            probe.measure(budget)
            if guard.host_evidence() != host:
                raise StepError("host-precondition", "reader namespace evidence changed")
        except BaseException as error:
            cause = error
            problems.append(_problem(probe.stage, error))
            problems.extend(_client_problems(error))
        finally:
            stage = "retire"
            retirement = Retirement(ResourceState.UNRESOLVED, probe.cid)
            retirement, observation_problem = probe.retire()
        if observation_problem is not None:
            if cause is None:
                cause = probe.retirement_cause
            problems.append(observation_problem)
    except BaseException as error:
        if cause is None:
            cause = error
        problems.append(_problem(stage, error))
        problems.extend(_client_problems(error))
    finally:
        if engine is not None:
            try:
                engine.close()
            except BaseException as error:
                if cause is None:
                    cause = error
                problems.append(_problem("close", error))
    if cancel is not None and cancel.is_set() and not any(p.category == "cancelled" for p in problems):
        problems.append(Problem("retire", "cancelled", "operation was cancelled before result delivery"))
    if problems or retirement.problems or retirement.state != ResourceState.RETIRED:
        primary = problems[0] if problems else retirement.problems[0]
        # silent-defaults: allow-contract -- The first available problem owns the primary failure; the immutable secondary inventory excludes exactly that problem; tests/test_operation.py::OperationTests.
        secondary = (*problems[1:], *retirement.problems) if problems else retirement.problems[1:]
        if cause is None and probe is not None:
            cause = probe.retirement_cause
        # silent-defaults: allow-contract -- No probe object means no ownership handle exists; report absence rather than fabricate recovery evidence; tests/test_operation.py::OperationTests.
        handle = None if probe is None else probe.handle()
        raise ProbeFailure(primary, Retirement(retirement.state, retirement.container_id, secondary), handle) from cause
    return ProbeResult(probe.observation.uid_map, probe.observation.gid_map,
                       retirement.container_id)
