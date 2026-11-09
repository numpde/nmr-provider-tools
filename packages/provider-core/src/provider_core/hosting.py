"""Science-independent requests and observations for one owned calculation.

These records do not contain scientific objects or Jobs authority. Backends
implement the protocols; the embedded example backend is in embedded_host.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Protocol

from provider_core.contribution import CallRef


def positive_seconds(value):
    if not (type(value) in (int, float) and math.isfinite(value) and 0 < value):
        raise ValueError("Execution allowances must be finite positive seconds.")


@dataclass(frozen=True, slots=True)
class ReservationRef:
    """A backend's assignment, usable only through its owning analysis."""

    host: str
    analysis: str
    allocation: str
    binding_id: str
    binding_revision: str


@dataclass(frozen=True, slots=True)
class OperationRef:
    host: str
    analysis: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class StateRef:
    host: str
    analysis: str
    allocation: str
    binding_revision: str
    state_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class ResultRef:
    operation: OperationRef
    result_id: str


@dataclass(frozen=True, slots=True)
class StateArgument:
    name: str
    reference: StateRef


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
        if not (type(self.target) is CallRef and type(self.payload) is bytes
                and type(self.states) is tuple and all(type(item) is StateArgument for item in self.states)
                and type(self.reservation) is ReservationRef and type(self.limits) is ExecutionLimits):
            raise TypeError("Invocation fields must use the declared immutable hosting records.")


@dataclass(frozen=True, slots=True)
class NotAdmitted:
    """This invocation caused no dispatch or orphaned resource acquisition."""

    reason: str


class RequestConflict(ValueError):
    """An operation ID already names different work; neither request is changed."""


class CapacityUnavailable(RuntimeError):
    """No analysis or reservation was acquired."""


class Outcome(Enum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Failure:
    """Protected original diagnostics, not an automatically publishable failure."""

    phase: str
    cause: BaseException = field(repr=False)


@dataclass(frozen=True, slots=True)
class Observation:
    revision: int
    dispatched: bool
    finished: bool
    outcome: Outcome | None
    result: ResultRef | None
    result_released: bool
    states: tuple[StateArgument, ...]
    failures: tuple[Failure, ...]
    stop_requested: bool
    allowance_expired: bool


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
