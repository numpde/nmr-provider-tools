"""Science-independent requests, grants and observations for owned calculations.

These records contain no scientific objects or Jobs authority. Execution backends
implement the protocols; scientific contributions implement provider_core.contribution.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
import math
import re
from typing import Protocol

from provider_core.contribution import (
    CallRef,
    ConcurrencyRequirement,
    InterruptionSupport,
    IsolationRequirement,
    ProgressEvent,
    ResourceRequirement,
)


def positive_seconds(value):
    if not (type(value) in (int, float) and math.isfinite(value) and 0 < value):
        raise ValueError("Execution allowances must be finite positive seconds.")


def _identity(value, label):
    if not (
        type(value) is str
        and value
        and all(character.isprintable() and not character.isspace() for character in value)
    ):
        raise ValueError(f"{label} must be a nonempty identity without whitespace or control characters")


def _digest(value, label):
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}\Z", value) is None:
        raise ValueError(f"{label} must be a lowercase sha256:<64 hex characters> digest")


@dataclass(frozen=True, slots=True)
class ResourceGrant:
    """Capacity that deployment has assigned to one execution allocation."""

    kind: str
    amount: int
    unit: str
    selector: str | None = None
    exclusive: bool = False

    def __post_init__(self):
        _identity(self.kind, "Resource grant kind")
        _identity(self.unit, "Resource grant unit")
        if not (type(self.amount) is int and 0 < self.amount):
            raise ValueError("Resource grant amount must be a positive integer")
        if self.selector is not None:
            _identity(self.selector, "Resource grant selector")
        if type(self.exclusive) is not bool:
            raise TypeError("Resource grant exclusivity must be boolean")

    @property
    def identity(self):
        return self.kind, self.unit, self.selector

    def covers(self, requirement: ResourceRequirement) -> bool:
        if type(requirement) is not ResourceRequirement:
            raise TypeError("Resource coverage requires a ResourceRequirement")
        return (
            self.identity == requirement.identity
            and self.amount >= requirement.amount
            and (not requirement.exclusive or self.exclusive)
        )


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Execution semantics a backend can enforce before accepting work."""

    contract_versions: tuple[int, ...]
    isolation: tuple[IsolationRequirement, ...]
    concurrency: tuple[ConcurrencyRequirement, ...]
    interruption: tuple[InterruptionSupport, ...]
    progress: bool

    def __post_init__(self):
        if not (
            type(self.contract_versions) is tuple
            and self.contract_versions
            and all(type(version) is int and 0 < version for version in self.contract_versions)
            and len(set(self.contract_versions)) == len(self.contract_versions)
        ):
            raise ValueError("Backend contract versions must be distinct positive integers")
        if not (
            type(self.isolation) is tuple
            and self.isolation
            and all(type(value) is IsolationRequirement for value in self.isolation)
            and len(set(self.isolation)) == len(self.isolation)
        ):
            raise TypeError("Backend isolation support must be a distinct immutable nonempty tuple")
        if not (
            type(self.concurrency) is tuple
            and self.concurrency
            and all(type(value) is ConcurrencyRequirement for value in self.concurrency)
            and len(set(self.concurrency)) == len(self.concurrency)
        ):
            raise TypeError("Backend concurrency support must be a distinct immutable nonempty tuple")
        if not (
            type(self.interruption) is tuple
            and self.interruption
            and all(type(value) is InterruptionSupport for value in self.interruption)
            and len(set(self.interruption)) == len(self.interruption)
        ):
            raise TypeError("Backend interruption support must be a distinct immutable nonempty tuple")
        if type(self.progress) is not bool:
            raise TypeError("Backend progress support must be boolean")


@dataclass(frozen=True, slots=True)
class AllocationRef:
    host: str
    allocation: str
    binding_id: str
    binding_revision: str

    def __post_init__(self):
        _identity(self.host, "Host identity")
        _identity(self.allocation, "Allocation identity")
        _identity(self.binding_id, "Binding identity")
        _digest(self.binding_revision, "Binding revision")


@dataclass(frozen=True, slots=True)
class ReservationRef:
    """A backend assignment usable only through its owning analysis."""

    host: str
    analysis: str
    allocation: str
    binding_id: str
    binding_revision: str

    def __post_init__(self):
        _identity(self.host, "Host identity")
        _identity(self.analysis, "Analysis identity")
        _identity(self.allocation, "Allocation identity")
        _identity(self.binding_id, "Binding identity")
        _digest(self.binding_revision, "Binding revision")


@dataclass(frozen=True, slots=True)
class OperationRef:
    host: str
    analysis: str
    operation_id: str

    def __post_init__(self):
        _identity(self.host, "Host identity")
        _identity(self.analysis, "Analysis identity")
        if not (type(self.operation_id) is str and 0 < len(self.operation_id) <= 160):
            raise ValueError("Operation identity must contain 1 through 160 characters")


@dataclass(frozen=True, slots=True)
class StateRef:
    host: str
    analysis: str
    allocation: str
    binding_id: str
    binding_revision: str
    state_id: str
    kind: str

    def __post_init__(self):
        _identity(self.host, "Host identity")
        _identity(self.analysis, "Analysis identity")
        _identity(self.allocation, "Allocation identity")
        _identity(self.binding_id, "Binding identity")
        _digest(self.binding_revision, "Binding revision")
        _identity(self.state_id, "State identity")
        _identity(self.kind, "Scientific state kind")


@dataclass(frozen=True, slots=True)
class ResultRef:
    operation: OperationRef
    result_id: str

    def __post_init__(self):
        if type(self.operation) is not OperationRef:
            raise TypeError("Result reference requires an OperationRef")
        _identity(self.result_id, "Result identity")


@dataclass(frozen=True, slots=True)
class StateArgument:
    name: str
    reference: StateRef

    def __post_init__(self):
        _identity(self.name, "State argument name")
        if type(self.reference) is not StateRef:
            raise TypeError("State argument requires a StateRef")


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Original allowances; observing an operation cannot renew them.

    Queue time starts at submission, execution at guarded dispatch, and cleanup
    when closing starts. Expiry requests stopping; it does not establish exit.
    """

    queue_seconds: float
    execution_seconds: float
    cleanup_seconds: float
    input_bytes: int
    result_bytes: int

    def __post_init__(self):
        for value in (self.queue_seconds, self.execution_seconds, self.cleanup_seconds):
            positive_seconds(value)
        if not all(type(value) is int and 0 < value for value in (self.input_bytes, self.result_bytes)):
            raise ValueError("Input and result allowances must be positive byte counts.")


@dataclass(frozen=True, slots=True)
class Invocation:
    operation_id: str
    target: CallRef
    payload: bytes = field(repr=False)
    states: tuple[StateArgument, ...]
    reservation: ReservationRef
    limits: ExecutionLimits

    def __post_init__(self):
        if not (type(self.operation_id) is str and 0 < len(self.operation_id) <= 160):
            raise ValueError("An invocation needs an explicit operation ID of 1 through 160 characters.")
        if not (
            type(self.target) is CallRef
            and type(self.payload) is bytes
            and type(self.states) is tuple
            and all(type(item) is StateArgument for item in self.states)
            and type(self.reservation) is ReservationRef
            and type(self.limits) is ExecutionLimits
        ):
            raise TypeError("Invocation fields must use the declared immutable hosting records.")


class AdmissionCode(Enum):
    CLOSED = "closed"
    CAPACITY = "capacity"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"
    INVALID_REFERENCE = "invalid_reference"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class NotAdmitted:
    """This invocation caused no dispatch or orphaned resource acquisition."""

    code: AdmissionCode
    message: str

    def __post_init__(self):
        if type(self.code) is not AdmissionCode:
            raise TypeError("Non-admission requires an AdmissionCode")
        if not (type(self.message) is str and self.message):
            raise ValueError("Non-admission requires an explanatory message")

    @property
    def reason(self):
        return self.message


class RequestConflict(ValueError):
    """An operation ID already names different work; neither request is changed."""


class CapacityUnavailable(RuntimeError):
    """No analysis or reservation was acquired."""


class AcceptanceState(Enum):
    OWNED = "owned"
    UNCERTAIN = "uncertain"


class DispatchState(Enum):
    QUEUED = "queued"
    STARTED = "started"
    NOT_STARTED = "not_started"
    UNKNOWN = "unknown"


class Outcome(Enum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


class ResultState(Enum):
    NONE = "none"
    AVAILABLE = "available"
    ENCODING_FAILED = "encoding_failed"
    RELEASED = "released"


class ResourceState(Enum):
    RESERVED = "reserved"
    OCCUPIED = "occupied"
    RELEASED = "released"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Failure:
    """Protected original diagnostics, not an automatically publishable failure."""

    phase: str
    cause: BaseException = field(repr=False)


@dataclass(frozen=True, slots=True)
class Observation:
    revision: int
    acceptance: AcceptanceState
    dispatch: DispatchState
    finished: bool
    outcome: Outcome | None
    result_state: ResultState
    result: ResultRef | None
    states: tuple[StateArgument, ...]
    progress: tuple[ProgressEvent, ...]
    progress_dropped: int
    failures: tuple[Failure, ...]
    stop_requested: bool
    allowance_expired: bool
    resources: ResourceState


@dataclass(frozen=True, slots=True)
class CleanupObservation:
    revision: int
    settled: bool
    allowance_expired: bool
    failures: tuple[Failure, ...]


class AnalysisCleanup(Protocol):
    async def observe(self, *, after: int | None, wait_seconds: float) -> CleanupObservation: ...


class ResultRelease(Protocol):
    async def observe(self, *, after: int | None, wait_seconds: float) -> CleanupObservation: ...


class InstanceRetirement(Protocol):
    async def observe(self, *, after: int | None, wait_seconds: float) -> CleanupObservation: ...


class Operation(Protocol):
    @property
    def reference(self) -> OperationRef: ...

    async def observe(self, *, after: int | None, wait_seconds: float) -> Observation: ...

    def request_stop(self) -> None: ...

    async def read_result(self, result: ResultRef) -> bytes: ...

    def release_result(self, result: ResultRef) -> ResultRelease: ...


class AnalysisExecution(Protocol):
    def submit(self, invocation: Invocation, *, dispatch_guard: Callable[[], None]) -> Operation | NotAdmitted: ...

    def close(self) -> AnalysisCleanup: ...


class PoolAdministration(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities: ...

    @property
    def allocations(self) -> tuple[AllocationRef, ...]: ...

    def retire_allocation(self, reference: AllocationRef) -> InstanceRetirement: ...

    def retire(self) -> InstanceRetirement: ...


async def observe_completion(operation: Operation) -> Observation:
    """Wait on an existing operation without submitting or stopping work.

    The caller owns this wait's timeout. Cancelling it leaves the original
    operation available for another observer; it does not establish that
    scientific effects have stopped. Completion preserves the independent
    scientific outcome, result availability and cleanup facts in Observation.
    """
    observation = await operation.observe(after=None, wait_seconds=0)
    while not observation.finished:
        observation = await operation.observe(
            after=observation.revision, wait_seconds=1,
        )
    return observation


async def observe_cleanup(
    control: AnalysisCleanup | InstanceRetirement | ResultRelease,
) -> CleanupObservation:
    """Wait on an existing cleanup request, propagating its original causes.

    Cancellation only ends this wait. An allowance expiring does not prove
    settlement, so observation continues until settlement or a reported failure.
    One failure propagates directly; independent failures retain their causes
    together. This helper never initiates or repeats cleanup itself.
    """
    observation = await control.observe(after=None, wait_seconds=0)
    while not observation.settled and not observation.failures:
        observation = await control.observe(
            after=observation.revision, wait_seconds=1,
        )
    causes = [failure.cause for failure in observation.failures]
    if len(causes) == 1:
        raise causes[0]
    if causes:
        raise BaseExceptionGroup("Execution cleanup reported multiple failures.", causes)
    return observation
