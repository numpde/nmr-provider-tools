"""Science-independent execution contracts and result-consumption ownership.

These records contain no scientific objects or Jobs authority. Execution backends
implement the protocols; scientific contributions implement provider_core.contribution.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
import re
import reprlib
from types import MappingProxyType
from typing import Never, Protocol
from uuid import uuid4

from provider_core.contribution import (
    CallRef,
    ConcurrencyRequirement,
    InterruptionSupport,
    IsolationRequirement,
    ProgressEvent,
)

from provider_core.task_boundary import contain_task_exit
from provider_core.tools import require_finite_number, require_positive_number
from provider_core.type_requirements import require_instance, require_type


def _identity(identity_text, identity_role):
    require_type(identity_text, str)

    if not (identity_text and all(((character.isprintable()) and (not character.isspace())) for character in identity_text)):
        raise ValueError(f"{identity_role=}; {reprlib.repr(identity_text)=} should be nonempty printable text without whitespace")


def _digest(digest_text, digest_role):
    require_type(digest_text, str)

    expected_digest_pattern = r"(?:sha256:|configured-v[23]:sha256:)[0-9a-f]{64}\Z"

    if re.fullmatch(expected_digest_pattern, digest_text) is None:
        raise ValueError(f"{digest_role=}; {reprlib.repr(digest_text)=} should match {expected_digest_pattern=}")


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Execution semantics a backend can enforce before accepting work."""

    contract_versions: frozenset[int]
    isolation: frozenset[IsolationRequirement]
    concurrency: frozenset[ConcurrencyRequirement]
    interruption: frozenset[InterruptionSupport]
    progress: bool

    def __post_init__(self):
        for (capability_name, supported_options, expected_type) in (
            ("contract_versions", self.contract_versions, int),
            ("isolation", self.isolation, IsolationRequirement),
            ("concurrency", self.concurrency, ConcurrencyRequirement),
            ("interruption", self.interruption, InterruptionSupport),
        ):
            require_type(
                supported_options,
                frozenset,
                error_factory=lambda diagnostic: TypeError(f"{capability_name=}; " + diagnostic),
            )

            if not (supported_options):
                raise ValueError(f"{capability_name=} should have at least one supported option")

            for supported_option in supported_options:
                require_type(
                    supported_option,
                    expected_type,
                    error_factory=lambda diagnostic: TypeError(f"{capability_name=}; " + diagnostic),
                )

        if any((version <= 0) for version in self.contract_versions):
            raise ValueError(f"Backend contract versions must be positive; {self.contract_versions=}")

        require_type(self.progress, bool)


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

        require_type(self.operation_id, str)

        if not (0 < len(self.operation_id) <= 160):
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
    producer: OperationRef

    def __post_init__(self):
        _identity(self.host, "Host identity")
        _identity(self.analysis, "Analysis identity")
        _identity(self.allocation, "Allocation identity")
        _identity(self.binding_id, "Binding identity")
        _digest(self.binding_revision, "Binding revision")
        _identity(self.state_id, "State identity")
        _identity(self.kind, "Scientific state kind")

        require_type(self.producer, OperationRef)

        if (self.producer.host, self.producer.analysis) != (self.host, self.analysis):
            raise ValueError("State producer must belong to the same host and analysis")


@dataclass(frozen=True, slots=True)
class ResultRef:
    operation: OperationRef
    result_id: str

    def __post_init__(self):
        require_type(self.operation, OperationRef)

        _identity(self.result_id, "Result identity")


@dataclass(frozen=True, slots=True)
class StateResult:
    """A named producer output; its name does not name a consumer input."""

    name: str
    reference: StateRef

    def __post_init__(self):
        _identity(self.name, "State output name")

        require_type(self.reference, StateRef)


@dataclass(frozen=True, slots=True)
class OperationPolicy:
    """Rules the SDK applies to scientific methods submitted by provider code.

    Provider code passes this record to submit_method or submit_call, or includes
    it in an Invocation. The SDK checks the request and available capacity before
    admitting it: creating an operation handle and reserving space for its work.

    The queue clock starts at admission. The execution clock replaces it when
    the executor passes execution's cancellation and start-deadline checks;
    it includes initialization, decoding, science and result encoding. Expiry
    requests stopping but cannot force the scientific thread to exit.

    Provider code starts cleanup by releasing a result or closing an analysis.
    Its cleanup clock only marks unfinished cleanup overdue; cleanup continues.
    Analysis close uses the shortest cleanup threshold among retained operations,
    or the host retirement threshold when it retains no operations.

    Oversized encoded input is refused before admission. The SDK reserves result
    space at admission and fails the operation if its encoded result exceeds it.
    Re-observing an operation neither restarts work nor renews its clocks.
    """

    queue_stop_request_seconds: float
    execution_stop_request_seconds: float
    cleanup_overdue_flag_seconds: float
    reject_encoded_input_over_bytes: int
    reject_encoded_result_over_bytes: int

    def __post_init__(self):
        for policy_seconds in (
            self.queue_stop_request_seconds,
            self.execution_stop_request_seconds,
            self.cleanup_overdue_flag_seconds,
        ):
            require_positive_number(policy_seconds)

        (
            reject_encoded_input_over_bytes,
            reject_encoded_result_over_bytes,
        ) = (
            self.reject_encoded_input_over_bytes,
            self.reject_encoded_result_over_bytes,
        )

        require_type(reject_encoded_input_over_bytes, int)

        if not (0 < reject_encoded_input_over_bytes):
            raise ValueError(f"{reject_encoded_input_over_bytes=} should be positive")

        require_type(reject_encoded_result_over_bytes, int)

        if not (0 < reject_encoded_result_over_bytes):
            raise ValueError(f"{reject_encoded_result_over_bytes=} should be positive")


class StartDeadlineExpired(TimeoutError):
    """The execution backend could not begin science within its original start window."""


@dataclass(frozen=True, slots=True)
class Invocation:
    """Data for one scientific-method submission, with an SDK-generated identity.

    Provider code submits this record through an execution session. Keep the same
    record when retrying transport delivery of that submission; constructing a new
    Invocation asks the SDK to perform separate work. start_not_after uses the
    addressed backend's event-loop clock, not a wall-clock or cross-host timestamp.
    It bounds entry to initialization and science; observing or retrying the same
    invocation cannot extend it. State input names map to portable references;
    construction snapshots that mapping so retries retain the same inputs.
    """

    operation_id: str = field(default_factory=lambda: uuid4().hex, kw_only=True)
    target: CallRef
    payload: bytes = field(repr=False)
    states: Mapping[str, StateRef]
    reservation: ReservationRef
    policy: OperationPolicy

    # None adds no external start deadline to the execution policy's clocks.
    start_not_after: float | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if self.start_not_after is not None:
            require_finite_number(self.start_not_after)

        require_type(self.operation_id, str)

        if not (0 < len(self.operation_id) <= 160):
            raise ValueError("An invocation needs an operation ID of 1 through 160 characters.")

        target_call_ref = self.target

        require_type(target_call_ref, CallRef)

        payload = self.payload

        require_type(payload, bytes)

        state_arguments = self.states

        require_instance(state_arguments, Mapping)

        state_arguments = dict(state_arguments)

        for (state_name, state_reference) in state_arguments.items():
            _identity(state_name, "State argument name")

            require_type(state_reference, StateRef)

        object.__setattr__(self, "states", MappingProxyType(state_arguments))

        reservation_ref = self.reservation

        require_type(reservation_ref, ReservationRef)

        operation_policy = self.policy

        require_type(operation_policy, OperationPolicy)


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
        require_type(self.code, AdmissionCode)

        require_type(self.message, str)

        if not (self.message):
            raise ValueError("Non-admission requires an explanatory message")

    def __str__(self) -> str:
        code = self.code.value
        message = self.message

        return f"{code=!s}; {message=!s}"

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
    operation: OperationRef
    target: CallRef
    revision: int
    acceptance: AcceptanceState
    dispatch: DispatchState
    finished: bool
    outcome: Outcome | None
    result_state: ResultState
    result: ResultRef | None
    states: tuple[StateResult, ...]
    progress: tuple[ProgressEvent, ...]
    progress_dropped: int
    failures: tuple[Failure, ...]
    stop_requested: bool
    allowance_expired: bool
    resources: ResourceState

    def require_result(self) -> ResultRef:
        """Return this observation's usable result or raise its original failures.

        Science can succeed while result encoding fails. A usable result requires
        completed successful science, available bytes and an actual result handle.
        This check neither waits nor releases anything. Workflows that need to
        inspect rejections themselves can keep using the observation's fields.
        """
        causes = [failure.cause for failure in self.failures]

        if len(causes) == 1:
            raise causes[0]

        if causes:
            raise BaseExceptionGroup("Scientific execution reported independent failures", causes)

        if not (
            self.finished and
            (self.outcome is Outcome.SUCCEEDED) and
            (self.result_state is ResultState.AVAILABLE) and
            (self.result is not None)
        ):
            raise RuntimeError(
                f"Cannot use the result of {self.operation=}; "
                f"{self.finished=}, {self.outcome=}, {self.result_state=}, {self.result=}"
            )

        return self.result


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
    """One execution session: stop scientific work before separately requesting cleanup.

    request_stop() refuses later submissions and signals unfinished operations.
    Retained results stay usable by receivers. close() separately requests cleanup;
    stopping does not require a caller-supplied permission callback.
    """

    def submit(self, invocation: Invocation) -> Operation | NotAdmitted: ...

    def request_stop(self) -> None: ...

    def close(self) -> AnalysisCleanup: ...


class PoolAdministration(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities: ...

    @property
    def allocations(self) -> frozenset[AllocationRef]: ...

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
            after=observation.revision,
            wait_seconds=1,
        )

    return observation


async def observe_cleanup(
    control: AnalysisCleanup | InstanceRetirement | ResultRelease,
) -> CleanupObservation:
    """Wait for cleanup already requested by provider code; raise its original failures.

    Pass the control returned by analysis.close(), operation.release_result() or
    host.retire(). This helper keeps waiting if cleanup becomes overdue: the flag
    alone does not end the wait or emit a warning. To react before cleanup finishes,
    call control.observe(...) and inspect the returned observation's
    allowance_expired field.

    Canceling this wait leaves cleanup running. A single cleanup failure is raised
    directly; independent failures keep their original causes together. Waiting
    again observes the same cleanup rather than starting it again.
    """
    observation = await control.observe(after=None, wait_seconds=0)

    while (not observation.settled) and (not observation.failures):
        observation = await control.observe(
            after=observation.revision,
            wait_seconds=1,
        )

    causes = [failure.cause for failure in observation.failures]

    if len(causes) == 1:
        raise causes[0]

    if causes:
        raise BaseExceptionGroup("Execution cleanup reported multiple failures.", causes)

    return observation


def _observe_owned_task(task: asyncio.Task) -> None:
    # Retrieve detached diagnostics without replacing the original task or cause.
    if not task.cancelled():
        task.exception()


def _start_owned_task[ResultT](operation: Callable[[], Awaitable[ResultT]]) -> asyncio.Task[ResultT]:
    """Start one contained task and retain its diagnostics even between observers."""
    coroutine = contain_task_exit(operation)

    try:
        task = asyncio.create_task(coro=coroutine)
    except BaseException:
        coroutine.close()
        raise

    task.add_done_callback(_observe_owned_task)

    return task


async def _join_settlement[ResultT](*, task: asyncio.Task[ResultT]) -> ResultT:
    """Defer observer cancellation until this exact cleanup task returns or fails."""
    interruptions = []
    waiter = asyncio.current_task()

    while True:
        cancellations_before_wait = waiter.cancelling()

        try:
            cleanup_result = await asyncio.shield(task)
        except asyncio.CancelledError as cause:
            # A caller interruption can race with child completion.
            # Retain it before observing the child's terminal result on the next
            # pass; the owned task's cancellation instead ends settlement.
            if waiter.cancelling() > cancellations_before_wait:
                interruptions.append(cause)
                continue

            if interruptions:
                raise BaseExceptionGroup(
                    "Cleanup cancellation and interrupted shutdown observation.",
                    [*interruptions, cause],
                )

            raise
        except BaseException as cause:
            if interruptions:
                raise BaseExceptionGroup(
                    "Cleanup failed while shutdown observation was interrupted.",
                    [*interruptions, cause],
                )

            raise
        else:
            if len(interruptions) == 1:
                raise interruptions[0]

            if interruptions:
                raise BaseExceptionGroup("Shutdown observation was interrupted.", interruptions)

            return cleanup_result


def _raise_context_failure(*, original: BaseException | None, cleanup: BaseException) -> Never:
    """Keep a lone original cause intact; group independent work and cleanup errors."""

    if (original is None) or (cleanup is original):
        raise cleanup

    raise BaseExceptionGroup(
        "Context work and SDK cleanup reported failures",
        [original, cleanup],
    ) from None


async def settle_cleanup(
    control: AnalysisCleanup | InstanceRetirement | ResultRelease,
) -> CleanupObservation:
    """Join already requested cleanup before propagating observer interruptions.

    Unlike observe_cleanup, this shutdown join may wait indefinitely. Repeated
    cancellation does not cancel cleanup or allow outer resources to close early.
    Once cleanup returns or reports failure, propagate its original causes and
    any observer interruptions. An overdue flag alone does not end settlement.
    This observes the supplied control; it never repeats a cleanup request.
    """

    return await _join_settlement(task=_start_owned_task(operation=partial(observe_cleanup, control=control)))


class ResultConsumption:
    """Own one receiver attempt and the subsequent release of an exact result.

    A successful receiver return means acceptance under that receiver's contract;
    it does not establish durability or publication. A receiver failure, including
    a lost acknowledgement, remains the original failure and never authorizes an
    automatic resend. The caller owns any reconciliation and receiver resources.

    Cancelling a waiter leaves acceptance running. Keep this owner and join its
    acceptance before closing resources the receiver or result reader may use.
    An async context joins it on exit, including after body failure or cancellation;
    entering the context does not start acceptance or release an unused result.
    """

    __slots__ = ("_operation", "_result_ref", "_receive", "_acceptance", "_release")

    def __init__(
        self,
        *,
        operation: Operation,
        result: ResultRef,
        receive: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._operation = operation
        self._result_ref = result
        self._receive = receive
        self._acceptance: asyncio.Task[None] | None = None
        self._release: ResultRelease | None = None

    async def __aenter__(self) -> ResultConsumption:
        """Retain this owner without starting its receiver."""

        return self

    async def __aexit__(self, exception_type, exception, traceback) -> None:
        """Settle before outer cleanup; preserve body and independent receiver failures."""

        try:
            await self.settle()
        except BaseException as cleanup:
            _raise_context_failure(original=exception, cleanup=cleanup)

    @property
    def operation(self) -> Operation:
        return self._operation

    @property
    def result(self) -> ResultRef:
        return self._result_ref

    @property
    def acceptance(self) -> asyncio.Task[None] | None:
        return self._acceptance

    @property
    def release(self) -> ResultRelease | None:
        return self._release

    async def _accept(self) -> None:
        payload = await self.operation.read_result(self.result)

        await self._receive(payload)

    def start(self) -> asyncio.Task[None]:
        """Retain the original read/receiver task before its first async effect."""

        if self.acceptance is None:
            self._acceptance = _start_owned_task(operation=self._accept)

        return self.acceptance

    async def wait_for_acceptance(self) -> None:
        """Join the original receiver attempt without requesting result release."""
        await asyncio.shield(self.start())

    async def consume(self) -> CleanupObservation:
        """Observe acceptance, then request and observe the exact result release.

        Known acceptance lives in the completed task even if requesting release
        raises. Re-observation retries only that exact idempotent release request,
        or observes the original release control once one has been returned.
        """
        await self.wait_for_acceptance()

        if self.release is None:
            self._release = self.operation.release_result(self.result)

        return await observe_cleanup(self.release)

    async def settle(self) -> CleanupObservation | None:
        """Finish one consumption observation before propagating cancellation.

        This shutdown join may wait indefinitely. It defers waiter cancellations
        until the original acceptance and exact release observation return or
        fail, preserving those interruptions alongside any original failure.
        Use consume() for an interruptible wait while retaining this owner and
        its dependent resources. An owner never started has nothing to join and
        returns None without invoking its receiver. A failed receiver is never
        retried here.
        """

        if self.acceptance is None:
            return None

        return await _join_settlement(task=_start_owned_task(operation=self.consume))
