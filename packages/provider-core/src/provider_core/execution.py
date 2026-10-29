"""Order one prepared execution and transfer its approved outcome to delivery.

NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_lifecycle.py::execute_prepared confirms running progress,
records execution entry and observes current state before invoking its live handle.
Readback is a snapshot, never an atomic grant against later server cancellation.
Its lines 1287-1340 also poll generation and observe before accepting candidates;
this executor extends that observation to asynchronous preparation as well.

The awaited condition seam follows Magnet 600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf,
magnet_provider/attempt_progress.py::AttemptProgressReporter.report_condition and
analysis_execution.py:307,379,393. Core retains phase authority and requires an
acknowledged receipt; it does not import Magnet's retry or duplicate suppression.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
import random
from builtins import BaseExceptionGroup
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from provider_core.task_boundary import contain_task_exit
from provider_core.analysis import AnalysisFailure, AnalysisSuccess, ObservedWorkerCrash, PreparedAnalysis, retire_session
from provider_core.api import ApiFailure
from provider_core.attempt_record import (
    AcknowledgedAttempt,
    RecoveryScope,
    LocalExecutionPhase,
    SuppressionReason,
    TerminalOperation,
    mark_execution_entered,
)
from provider_core.attempt_request import complete_body, fail_body
from provider_core.attempt_response import JOB_STATES
from provider_core.delivery_client import DeliveryUnavailable
from provider_core.delivery_store import DeliveryFull, DeliveryOutcome
from provider_core.delivery_worker import require_delivery_retry_settings
from provider_core.job_input import VerifiedJobInput
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint, EnteredCheckpoint,
    EvidenceDigest, SuppressedCheckpoint, TerminalSelectedCheckpoint,
)
from provider_core.transport import ExchangeFailed


class _Retirement(Enum):
    NOT_ATTEMPTED = "not_attempted"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"


class ExecutionRefused(RuntimeError):
    """No scientific execution was invoked by this refusal."""


class ExecutionStopRequested(ExecutionRefused):
    """The actual supervising owner withdrew permission to continue work."""


def require_observation_gap(seconds):
    if not (type(seconds) in (int, float) and math.isfinite(seconds) and 0 < seconds):
        raise ValueError("Observation gap_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class ObservationRecovery:
    """Bound one fresh-observation wait and report its protected outage context.

    Observers are bounded synchronous callbacks. The executor supplies its current
    acknowledgement, not a constructed or inferred Job/Attempt identity.
    """

    gap_seconds: float
    backoff_max_seconds: float
    on_retry: Callable[[AcknowledgedAttempt, ApiFailure, float], None] = field(repr=False)
    on_recovered: Callable[[AcknowledgedAttempt, ApiFailure], None] = field(repr=False)

    def __post_init__(self):
        require_observation_gap(self.gap_seconds)
        value = self.backoff_max_seconds
        if not (type(value) in (int, float) and math.isfinite(value) and 0 < value):
            raise ValueError("Observation backoff_max_seconds must be finite and positive")
        if not callable(self.on_retry) or not callable(self.on_recovered):
            raise ValueError("Observation recovery requires explicit protected retry and recovery observers")


@dataclass(frozen=True, slots=True)
class ConditionRecovery:
    """Bound retries of one still-current awaited condition, requiring its receipt.

    Composition must own the only progress writer for this Attempt. Local claims
    and sequential session calls do not establish distributed writer exclusion.
    """

    window_seconds: float
    backoff_max_seconds: float
    on_retry: Callable[[AcknowledgedAttempt, str, str | None, ApiFailure, float], None] = field(repr=False)
    on_recovered: Callable[[AcknowledgedAttempt, str, str | None, ApiFailure], None] = field(repr=False)

    def __post_init__(self):
        for name, value in (("window_seconds", self.window_seconds), ("backoff_max_seconds", self.backoff_max_seconds)):
            if not (type(value) in (int, float) and math.isfinite(value) and 0 < value):
                raise ValueError(f"Condition recovery {name} must be finite positive seconds")
        if not callable(self.on_retry) or not callable(self.on_recovered):
            raise ValueError("Condition recovery requires protected retry and recovery observers")


class ConditionRecoveryExpired(TimeoutError):
    """Retry admission ended; original report evidence and effects stay owned."""

    def __init__(self, window_seconds, *, phase, condition_code, cause):
        super().__init__(
            f"Cannot confirm the current analysis condition: its {window_seconds:g}-second retry window expired; "
            "retain the original API evidence and settle work before recovery"
        )
        self.window_seconds, self.phase, self.condition_code = window_seconds, phase, condition_code
        self.cause = cause


def require_condition_recovery(policy):
    if policy is not None and type(policy) is not ConditionRecovery:
        raise ValueError("Condition recovery must be an admitted policy or explicit fail-stop None")


class ObservationGapExpired(TimeoutError):
    """The observation waiter expired; original API/work ownership is unchanged."""

    def __init__(self, gap_seconds, *, executor):
        # silent-defaults: allow-contract -- Diagnostic wording identifies whether the original server read had started.
        blocked = (
            "after starting a server read" if executor._observation_read_started else "while waiting for API exchange access"
        )
        super().__init__(
            f"Cannot obtain a fresh Attempt observation within {gap_seconds:g} seconds {blocked}; "
            "retain existing API owners and retire the original work before recovery"
        )
        self.gap_seconds, self._executor = gap_seconds, executor

    @property
    def cause(self):
        # This expired executor cannot begin another observation. Borrow its
        # actual window evidence so late read retirement can add its cause;
        # expiry while waiting for the lock has no invented read failure.
        return self._executor._observation_failure

    @property
    def read_started(self):
        return self._executor._observation_read_started

    @property
    def observation_pending(self):
        task = self._executor._observation_wait
        return task is not None and not task.done()


def require_observation_recovery(policy):
    if policy is not None and type(policy) is not ObservationRecovery:
        raise ValueError("Observation recovery must be an admitted policy or explicit fail-stop None")


def require_phase_progress_observer(observer):
    if observer is not None and not callable(observer):
        raise ValueError("Phase-progress continuation requires a diagnostic observer or explicit fail-stop None")


class _ConditionReporter:
    """A condition capability that expires with its one session invocation."""

    def __init__(self, executor, phase):
        self.executor, self.phase = executor, phase
        self.execution_attempt_ref = executor._record.admission.execution_attempt_ref
        self.active = True
        self.failure = None
        self.reporting = False

    def require_active(self):
        if not self.active:
            raise ExecutionRefused("Cannot continue analysis: its session phase has ended")
        if self.failure is not None:
            raise self.failure

    def require_execution(self):
        """Check the actual phase owner before the caller commits another effect."""
        self.require_active()
        try:
            if self.reporting:
                raise ExecutionRefused("Cannot continue analysis while its condition report is awaiting completion")
            self.executor._require_live_permission()
            deadline = self.executor._observation_deadline
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise ObservationGapExpired(self.executor._observation_recovery.gap_seconds, executor=self.executor)
        except Exception as cause:
            # A caught refusal must not let the binding return an otherwise
            # publishable outcome. Keep this same original phase failure.
            self.failure = cause
            self.executor._remember(cause)
            raise

    async def __call__(self, condition_code):
        self.require_active()
        if self.reporting:
            # Sequential calls are part of AnalysisSession's contract. Poison
            # both callers before any old condition can be retried or accepted.
            self.failure = ExecutionRefused("Cannot report concurrent analysis conditions; await the current report first")
            self.executor._remember(self.failure)
            raise self.failure
        self.reporting = True
        try:
            await contain_task_exit(self.executor._report_condition, self, condition_code)
        except (Exception, BaseExceptionGroup, asyncio.CancelledError) as cause:
            # Waiting for the API lock is part of the awaited report too. Keep
            # its first failure even when a binding catches the interruption.
            if self.active and self.failure is None:
                self.failure = cause
                self.executor._remember(cause)
            raise
        finally:
            self.reporting = False


class ExecutionStopped(RuntimeError):
    def __init__(self, observation):
        super().__init__(
            f"Cannot proceed with analysis: observed Attempt {observation.state} and Job {observation.job_state}; preserve the current record"
        )
        self.observation = observation


class ExecutionFailed(RuntimeError):
    """Protected primary/outcome/cleanup plus a borrowed acknowledgement.

    The acknowledgement identifies the affected Job and Attempt even before
    scientific work starts. Its presence does not prove dispatch, storage or
    terminal acceptance.
    """

    def __init__(self, *, cause, outcome, api_failure, cleanup_failures, attempt):
        super().__init__(
            "Analysis lifecycle did not settle; retain this owner and inspect delivery and cleanup evidence before reuse"
        )
        self.cause = cause
        self.outcome = outcome
        self.api_failure = api_failure
        self.cleanup_failures = cleanup_failures
        self.attempt = attempt
        self.operation_interruption = None
        self.interruption_cause = None
        self.condition_failure = self.latest_condition_failure = self.condition_receipt = None
        self.delivery_acceptance = self.delivery_failure = None
        self.delivery_diagnostic_failure = None
        self.handoff_interruption = None


def _suppression_reason(cause):
    if isinstance(cause, ExecutionStopped):
        return SuppressionReason.SERVER_STOP
    if isinstance(cause, (ApiFailure, ObservationGapExpired, ConditionRecoveryExpired)):
        return SuppressionReason.OBSERVATION_FAILED
    if isinstance(cause, (asyncio.CancelledError, ExecutionStopRequested)):
        return SuppressionReason.CALLER_CANCELLED
    return None


def _observe_task_failure(task):
    # The retained task still owns its result. Retrieve exceptions to prevent
    # abandoned tasks from writing private failures to the event-loop log.
    if not task.cancelled():
        task.exception()


def require_execution_settings(*, allowed_job_states, poll_seconds, task_settle_seconds):
    """Validate before either a controller or executor accepts its session."""
    if (
        type(allowed_job_states) is not frozenset
        or not allowed_job_states
        or not allowed_job_states <= JOB_STATES
        or "cancelled" in allowed_job_states
    ):
        raise ValueError(
            "Cannot configure execution: allowed_job_states must be a nonempty frozenset of open or closed; cancelled Jobs stop work"
        )
    for name, seconds in (("poll_seconds", poll_seconds), ("task_settle_seconds", task_settle_seconds)):
        if not (type(seconds) in (int, float) and math.isfinite(seconds) and 0 < seconds):
            raise ValueError(f"Cannot configure execution: {name} must be finite and positive")


class AttemptExecutor:
    """Own one run; settlement never resumes preparation or scientific execution.

    Session ownership transfers after local preconditions and claim acquisition.
    A precondition refusal leaves the supplied session with its caller. After that
    point all failure paths attempt retirement, retaining uncertainty on failure.

    Composition explicitly chooses allowed Job states. SECS
    34f5915c1534278bbf61b4c6592afa44cf94ab08,
    src/secs_inference/provider/analysis_run.py:260-265 continues a closed Job's
    active Attempt; NMRNet chooses open only. Job cancellation and terminal
    Attempts always stop. None keeps fail-stop observation handling; a supplied
    ObservationRecovery permits only bounded, settled read recovery. It cannot
    retry progress, START, terminal publication or scientific operations.
    """


    @classmethod
    # None preserves the post-START input read; Magnet supplies its verified object.
    def live(cls, api, claim, session, *, scope, preverified_input=None, **settings):
        return cls(api, scope, claim, session, preverified_input=preverified_input, **settings)

    # Core consumers keep fail-stop reads and phase delivery unless they explicitly supply recovery or a protected unconfirmed-phase observer.
    def __init__(
        self,
        api,
        scope,
        claim,
        session,
        *,
        preverified_input,
        delivery,
        on_delivery_retry,
        delivery_retry_initial_seconds,
        delivery_retry_max_seconds,
        allowed_job_states,
        poll_seconds,
        task_settle_seconds,
        observation_recovery=None,
        on_unconfirmed_phase_progress=None,
        condition_recovery=None,
    ):
        claim.require_live(scope)
        if type(scope) is not RecoveryScope or api.origin != scope.api_origin:
            raise ValueError("Cannot configure execution: signing origin differs from the selected scope")
        if preverified_input is not None and type(preverified_input) is not VerifiedJobInput:
            raise ValueError("Execution requires the actual verified Job input or an explicit post-START read")
        if ((delivery.endpoint.api_origin, delivery.endpoint.provider_ref)
                != (scope.api_origin, scope.provider_ref) or not callable(on_delivery_retry)):
            raise ValueError("Execution requires the scoped delivery binding and its protected retry observer.")
        require_execution_settings(
            allowed_job_states=allowed_job_states, poll_seconds=poll_seconds, task_settle_seconds=task_settle_seconds
        )
        require_delivery_retry_settings(delivery_retry_initial_seconds, delivery_retry_max_seconds)
        require_observation_recovery(observation_recovery)
        require_phase_progress_observer(on_unconfirmed_phase_progress)
        require_condition_recovery(condition_recovery)
        self.api, self.claim, self.session = api, claim, session
        self.scope, self._preverified_input = scope, preverified_input
        self.delivery, self._on_delivery_retry = delivery, on_delivery_retry
        self._delivery_retry_initial = delivery_retry_initial_seconds
        self._delivery_retry_max = delivery_retry_max_seconds
        self._handoff_stopping = asyncio.Event()
        self._handoff_interruption = None
        self._allowed_job_states = allowed_job_states
        self._poll_seconds = poll_seconds
        self._task_settle_seconds = task_settle_seconds
        self._observation_recovery = observation_recovery
        self._on_unconfirmed_phase_progress = on_unconfirmed_phase_progress
        self._condition_recovery = condition_recovery
        self._condition_failure = self._latest_condition_failure = self._condition_receipt = None
        self._task = None
        self._observation_wait = None
        self._observation_deadline = None
        self._observation_failure = None
        self._observation_read_started = False
        self._cancel_requested = False
        self._started = False
        self._active = False
        self._observing = False
        self._record = None
        self._acknowledged = None
        self._checkpoint = None
        self._outcome = None
        self._observed_crash = None
        self._crash_notice = asyncio.Event()
        self._delivery_outcome = self._delivery_acceptance = None
        self._delivery_diagnostic_failure = None
        self._handoff_wait = self._retirement_task = None
        self._api_failure = None
        self._operation_interruption = None
        self._api_lock = asyncio.Lock()
        self._retirement = _Retirement.NOT_ATTEMPTED
        self.last_failure = None

    @property
    def retirement_confirmed(self):
        """Expose the owning confirmation without asking composition to repeat it."""
        return self._retirement is _Retirement.CONFIRMED

    @property
    def observed_crash(self):
        """Expose the admitted protected cause independently of reporting effects."""
        return self._observed_crash

    @property
    def delivery_diagnostic_failure(self):
        """Retain the first crash-report observer failure without vetoing FAIL."""
        return self._delivery_diagnostic_failure

    @property
    def work_settled(self):
        """Confirm original task/I/O/native cleanup, independently of retained reports."""
        return (
            not self._observing
            and self._retirement is _Retirement.CONFIRMED
            and self._task is None
            and self._observation_wait is None
            and self._handoff_wait is None
            and self._retirement_task is None
            and self.delivery.retirement_confirmed
            and self.api.pending_owner is not self

        )

    @property
    def shutdown_ready(self):
        """Confirm cleanup and that no unhanded outcome/report would be lost."""
        return self.work_settled and self._outcome is None and self._delivery_outcome is None


    def require_terminal_release_ready(self, claim):
        if (claim is not self.claim or self._delivery_acceptance is None
                or type(self._checkpoint) is not DeliveryAcceptedCheckpoint):
            raise ExecutionRefused("Cannot release report capacity before this executor receives its matching delivery acknowledgement")

    async def run(self, acknowledged):
        if self._started:
            raise ExecutionRefused("This executor already accepted a run; settlement cannot authorize another execution")
        if (
            type(acknowledged) is not AcknowledgedAttempt
            or acknowledged.local_phase is not LocalExecutionPhase.NOT_ENTERED
            or acknowledged.admission.state != "in_progress"
            or acknowledged.start.scope != self.scope
        ):
            raise ExecutionRefused("Execution requires a matching active acknowledgement that has never entered science")
        self.claim.require_start(acknowledged.start)
        self.claim.require(self.scope, acknowledged.admission.execution_attempt_ref)
        self.claim.lifecycle_owner.require_execution_ready(self.claim, self, acknowledged)
        if self._preverified_input is not None and self._preverified_input.identity != acknowledged.start.input:
            raise ExecutionRefused("Verified Job input differs from this Attempt's selected input; no science was started")
        self.claim.begin(self)
        self._started = self._active = self._observing = True
        self._record = acknowledged
        self._acknowledged = acknowledged
        self._checkpoint = AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission)
        try:
            return await contain_task_exit(self._run_effects, acknowledged)
        except asyncio.CancelledError as cause:
            # A native cleanup coroutine can itself finish cancelled. Only a
            # cancellation directed at this caller withdraws handoff authority;
            # failed cleanup cannot veto an already-approved terminal outcome.
            if asyncio.current_task().cancelling():
                self._cancel_handoff()
            self._remember(cause)
            try:
                await self._retire_after_failure()
            except asyncio.CancelledError:
                # Cleanup has already retained its own cancellation. Preserve
                # the original caller interruption and every independent cause.
                self._link_interruption(cause)
                raise cause from self.last_failure
            if self._interruption_has_evidence():
                self._link_interruption(cause)
            raise
        except (Exception, BaseExceptionGroup) as cause:
            # This is the execution boundary: retain even unexpected binding or
            # codec failures, stop owned work, and never classify them as bad input.
            # An approved handoff remains independently owned if cleanup fails;
            # settlement observes that original delivery rather than vetoing it.
            self._remember(cause)
            try:
                cleanup_fatal = await self._retire_after_failure()
            except asyncio.CancelledError as interruption:
                self._link_interruption(interruption)
                raise
            if not isinstance(cause, Exception):
                raise BaseExceptionGroup("Analysis lifecycle raised a process-control exception", (cause, self.last_failure)) from None
            if cleanup_fatal is not None:
                raise BaseExceptionGroup("Analysis cleanup raised a process-control exception", (cleanup_fatal, self.last_failure)) from None
            raise self.last_failure from None
        finally:
            self._observing = False
            self._refresh_failure()
            self._finish_if_settled()

    async def _run_effects(self, acknowledged):
        await self._observe_active()
        instructions = self._preverified_input
        if instructions is None:
            instructions = await self._api(
                self.api.job_input, acknowledged.start.input, analysis_kind_ref=acknowledged.start.analysis_kind_ref
            )
        await self._progress("preparing")
        prepared = await self._monitor("preparing", self._prepare_session, acknowledged, instructions)
        if type(prepared) is AnalysisFailure:
            self._outcome = prepared
        else:
            if type(prepared) is not PreparedAnalysis:
                raise TypeError(
                    "Cannot execute analysis: preparation must return its handle and protected candidate identity."
                )
            entered = mark_execution_entered(acknowledged, prepared.identity)
            await self._progress("running")
            await self._write_entry(entered)
            await self._observe_active()
            await self._monitor("running", self._execute_session, prepared)
        # Both original tasks start before either is observed. Relay
        # backpressure cannot defer native cleanup.
        self._begin_retirement()
        self._handoff_wait = asyncio.create_task(contain_task_exit(self._handoff))
        self._handoff_wait.add_done_callback(_observe_task_failure)
        await asyncio.wait({self._handoff_wait, self._retirement_task}, timeout=self._task_settle_seconds,
                           return_when=asyncio.FIRST_EXCEPTION)
        if self._handoff_wait.done():
            await self._settle_handoff()
        await self._retire_session()
        if self._handoff_wait is not None:
            await asyncio.shield(self._handoff_wait)
            await self._settle_handoff()
        return self._checkpoint

    async def _prepare_session(self, acknowledged, instructions, *, report_condition):
        prepared = await self.session.prepare(acknowledged, instructions, report_condition=report_condition)
        if type(prepared) is AnalysisFailure:
            self._outcome = prepared
            self._refresh_failure()
        return prepared

    async def _execute_session(self, prepared, *, report_condition):
        outcome = await self.session.execute(prepared, report_condition=report_condition)
        # Capture before any final read or caught report failure can compete
        # with the returned science. Retention grants no publication authority.
        self._outcome = outcome
        self._refresh_failure()
        return outcome

    async def _call_session(self, operation, reporter, *args):
        try:
            try:
                reporter.require_execution()
                result = await contain_task_exit(operation, *args, report_condition=reporter)
            except (Exception, BaseExceptionGroup, asyncio.CancelledError) as cause:
                if reporter.failure is not None and cause is not reporter.failure:
                    # Re-raising inside this handler keeps the later binding
                    # failure as context. An explicit chain would overwrite the
                    # report's original cancellation or transport cause.
                    raise reporter.failure  # noqa: B904
                if type(cause) is not ObservedWorkerCrash:
                    raise
                result = self._admit_worker_crash(cause, reporter)
            if reporter.failure is not None:
                raise reporter.failure
            return result
        except asyncio.CancelledError as interruption:
            # Task.result() can consume the original cancellation. Capture it
            # in its coroutine, before either independent join observes it.
            self._operation_interruption = interruption
            self._refresh_failure()
            if (
                self.last_failure is not None
                and isinstance(self.last_failure.cause, asyncio.CancelledError)
                and self._interruption_has_evidence()
            ):
                self._link_interruption(self.last_failure.cause)
            raise
        finally:
            reporter.active = False

    def _admit_worker_crash(self, crash, reporter):
        reporter.require_active()
        self._require_live_permission()
        native = crash.native_exit
        if (
            crash.owner is not self.session or crash.acknowledged is not self._acknowledged
            or crash.phase != reporter.phase or self._cancel_requested
            or asyncio.current_task().cancelling()
            or self._observed_crash is not None or self._outcome is not None
            or type(self._checkpoint) not in (AcknowledgedCheckpoint, EnteredCheckpoint)
            or crash.runner.original_exit is not native
            or (native.role, native.generation, native.operation)
            != (crash.runner.role, crash.runner.generation, crash.runner.operation)
            or (self._observation_deadline is not None
                and asyncio.get_running_loop().time() >= self._observation_deadline)
        ):
            raise ExecutionRefused("Cannot report this crash: its original Attempt, active phase or native worker association does not match this owner")
        outcome = crash.public_failure()
        self._observed_crash = crash
        self._outcome = outcome
        self._crash_notice.set()
        self._refresh_failure()
        return self._outcome

    def _admitted_crash_result(self, phase):
        task = self._task
        return (
            self._observed_crash is not None and self._observed_crash.phase == phase
            and task is not None and task.done() and not task.cancelled()
            and task.exception() is None and task.result() is self._outcome
        )

    async def _monitor(self, phase, operation, *args):
        self._require_live_permission()
        self._cancel_requested = False
        reporter = _ConditionReporter(self, phase)
        self._task = asyncio.create_task(contain_task_exit(self._call_session, operation, reporter, *args))
        self._task.add_done_callback(_observe_task_failure)
        try:
            while not self._task.done():
                await asyncio.wait((self._task,), timeout=self._poll_seconds)
                if not self._task.done():
                    self._require_live_permission()
                    try:
                        await self._observe_active()
                    except (ApiFailure, ObservationGapExpired, ExchangeFailed, ObservedWorkerCrash) as failure:
                        # A GET outage cannot veto an already admitted local crash.
                        # Real server-stop evidence and earlier report failures still veto.
                        if (not self._admitted_crash_result(phase)
                                or (isinstance(failure, ObservedWorkerCrash) and failure is not self._observed_crash)):
                            raise
                        self._require_live_permission()
            task = self._task
            self._task = None
            result = task.result()
            self._require_live_permission()
            if self._observed_crash is not None and self._observed_crash.phase == phase:
                return result
            # A known binding failure needs no acceptance read. For a successful
            # return, finish observation before accepting the handle or outcome.
            await self._observe_active()
            self._require_live_permission()
            return result
        finally:
            reporter.active = False

    def _require_live_permission(self):
        self.claim.lifecycle_owner.require_execution_ready(self.claim, self, self._acknowledged)

    def _cancel_task(self):
        if self._task is not None and not self._task.done() and not self._cancel_requested:
            self._cancel_requested = True
            self._task.cancel()

    async def _settle_task(self):
        if self._task is None:
            return
        await asyncio.wait((self._task,), timeout=self._task_settle_seconds)
        if not self._task.done():
            raise TimeoutError("Analysis task has not stopped; retain ownership and settle the original task")
        task = self._task
        self._task = None
        if not task.cancelled():
            task.result()

    async def _api(self, operation, *args, **kwargs):
        async with self._api_lock:
            return await self._exchange_api(operation, *args, **kwargs)

    async def _report_condition(self, reporter, condition_code):
        """Retry only this awaited current fact while the monitor remains the sole reader."""
        policy = self._condition_recovery
        deadline = None
        # silent-defaults: allow-contract -- Without an admitted recovery policy there is no retry backoff.
        delay = min(self._poll_seconds, policy.backoff_max_seconds) if policy is not None else None
        self._condition_failure = self._latest_condition_failure = self._condition_receipt = None
        while True:
            async with self._api_lock:
                reporter.require_active()
                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    raise ConditionRecoveryExpired(
                        policy.window_seconds,
                        phase=reporter.phase,
                        condition_code=condition_code,
                        cause=self._latest_condition_failure,
                    ) from None
                try:
                    receipt = await self._exchange_api(
                        self.api.progress_attempt,
                        reporter.execution_attempt_ref,
                        phase=reporter.phase,
                        condition_code=condition_code,
                    )
                except asyncio.CancelledError:
                    # Capture only an entered condition exchange, not a waiter
                    # cancelled before acquiring the API lock. Monitor recovery
                    # can later replace the shared API failure.
                    self._latest_condition_failure = self.api.last_failure
                    raise
                except ApiFailure as failure:
                    if policy is None:
                        raise
                    # Monitor reads may replace the shared API failure; retain
                    # this unresolved report's latest send independently.
                    self._latest_condition_failure = failure
                    if deadline is None:
                        deadline = asyncio.get_running_loop().time() + policy.window_seconds
                    # Polls cannot overtake unsettled PUT I/O or replace the
                    # API's current-failure evidence before admission is decided.
                    await self.api.retire(owner=self)
                    if not self.api.can_retry_condition(failure, execution_attempt_ref=reporter.execution_attempt_ref):
                        raise
                    if self._condition_failure is None:
                        self._condition_failure = failure
                else:
                    self._condition_receipt = receipt
                    reporter.require_active()
                    break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ConditionRecoveryExpired(
                    policy.window_seconds,
                    phase=reporter.phase,
                    condition_code=condition_code,
                    cause=self._latest_condition_failure,
                ) from None
            seconds = min(remaining, random.uniform(delay / 2, delay))
            policy.on_retry(self._record, reporter.phase, condition_code, self._latest_condition_failure, seconds)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ConditionRecoveryExpired(
                    policy.window_seconds,
                    phase=reporter.phase,
                    condition_code=condition_code,
                    cause=self._latest_condition_failure,
                ) from None
            # Release the lock during pacing. The existing monitor can observe
            # server stop and cancel this same session task; no second reader.
            await asyncio.sleep(min(seconds, remaining))
            delay = min(policy.backoff_max_seconds, delay * 2)
        if self._condition_failure is not None:
            policy.on_recovered(self._record, reporter.phase, condition_code, self._condition_failure)
            if self._api_failure is self._latest_condition_failure:
                self._api_failure = None
        self._condition_failure = self._latest_condition_failure = self._condition_receipt = None

    async def _exchange_api(self, operation, *args, **kwargs):
        """Capture API evidence while the caller holds this executor's API lock."""
        self._require_live_permission()
        try:
            return await operation(*args, **kwargs, owner=self)
        except asyncio.CancelledError:
            self._api_failure = self.api.last_failure
            raise
        except ApiFailure as cause:
            self._api_failure = cause
            raise

    async def _observe_active(self):
        recovery = self._observation_recovery
        if recovery is None:
            observed = await self._api(
                self.api.read_attempt, self._record.admission.execution_attempt_ref, job_ref=self._record.start.input.job_ref
            )
            self._require_observed_active(observed)
        else:
            self._observation_failure = None
            self._observation_read_started = False
            deadline = asyncio.get_running_loop().time() + recovery.gap_seconds
            self._observation_deadline = deadline
            self._observation_wait = asyncio.create_task(contain_task_exit(self._observe_with_interruption_evidence, deadline))
            self._observation_wait.add_done_callback(_observe_task_failure)
            try:
                # wait() returns without awaiting cancellation cleanup. HTTP's
                # retirement grace must not postpone scientific retirement.
                # https://docs.python.org/3.12/library/asyncio-task.html#waiting-primitives
                done, _ = await asyncio.wait(
                    {self._observation_wait}, timeout=max(0, deadline - asyncio.get_running_loop().time())
                )
                if not done:
                    self._cancel_observation_wait()
                    raise ObservationGapExpired(recovery.gap_seconds, executor=self) from None
                task = self._observation_wait
                self._observation_wait = None
                observed = task.result()
                if asyncio.get_running_loop().time() >= deadline:
                    raise ObservationGapExpired(recovery.gap_seconds, executor=self) from None
            except asyncio.CancelledError:
                self._cancel_observation_wait()
                raise
            finally:
                if self._observation_wait is None:
                    self._observation_deadline = None

    def _require_observed_active(self, observed):
        # Terminal Attempts and cancelled Jobs stop work. Job closure policy is
        # explicit in composition: e.g. SECS continues closed Jobs, NMRNet stops.
        if observed.state != "in_progress" or observed.job_state not in self._allowed_job_states:
            raise ExecutionStopped(observed)

    async def _observe_with_interruption_evidence(self, deadline):
        try:
            return await self._observe_until_available(deadline)
        except asyncio.CancelledError as interruption:
            # The task's cancelled state alone cannot retain its original nested
            # cleanup cause. Keep that independent evidence with this executor.
            if interruption.__cause__ is not None:
                self._remember(interruption, cleanup=True)
            raise

    async def _observe_until_available(self, deadline):
        """Keep the same exchange lock through retirement, pacing and fresh reads.

        Queued progress must not overtake unresolved observation I/O. Science may
        finish in its existing task, but accepting it still requires a fresh read.
        The caller stops waiting at its deadline and retains this original task;
        cancellation/elapsed grace never establishes that the read has stopped.
        """
        recovery = self._observation_recovery
        delay, outage = min(self._poll_seconds, recovery.backoff_max_seconds), None
        async with self._api_lock:
            while True:
                if self._observed_crash is not None:
                    raise self._observed_crash
                if asyncio.get_running_loop().time() >= deadline:
                    raise ObservationGapExpired(recovery.gap_seconds, executor=self) from None
                try:
                    self._observation_read_started = True
                    observed = await self._exchange_api(
                        self.api.read_attempt,
                        self._record.admission.execution_attempt_ref,
                        job_ref=self._record.start.input.job_ref,
                    )
                except asyncio.CancelledError:
                    self._observation_failure = self.api.last_failure
                    raise
                except ApiFailure as failure:
                    self._observation_failure = failure
                    if self._observed_crash is not None:
                        raise
                    await self.api.retire(owner=self)
                    if self._observed_crash is not None:
                        raise
                    if not self.api.can_retry_read(failure):
                        raise
                    outage = failure
                    now = asyncio.get_running_loop().time()
                    remaining = deadline - now
                    if remaining <= 0:
                        raise ObservationGapExpired(recovery.gap_seconds, executor=self) from None
                    wait = random.uniform(min(delay, remaining) / 2, min(delay, remaining))
                    advised = failure.retry_after_seconds
                    if advised is not None:
                        wait = max(wait, advised)
                    retry_at = now + wait
                    if retry_at < deadline:
                        recovery.on_retry(self._record, failure, wait)
                    wait = min(retry_at, deadline) - asyncio.get_running_loop().time()
                    if wait <= 0:
                        continue
                    try:
                        await asyncio.wait_for(self._crash_notice.wait(), timeout=wait)
                    except TimeoutError:
                        delay = min(recovery.backoff_max_seconds, delay * 2)
                        continue
                    raise self._observed_crash
                else:
                    # A received stop still applies at the deadline or after a crash.
                    self._require_observed_active(observed)
                    if self._observed_crash is not None:
                        return observed
                    self._observation_failure = None
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ObservationGapExpired(recovery.gap_seconds, executor=self) from None
                    if outage is not None:
                        recovery.on_recovered(self._record, outage)
                        if self._api_failure is outage:
                            self._api_failure = None
                    return observed

    def _cancel_observation_wait(self):
        task = self._observation_wait
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()

    async def _settle_observation_wait(self):
        if self._observation_wait is None:
            return
        await asyncio.wait({self._observation_wait}, timeout=self._task_settle_seconds)
        if not self._observation_wait.done():
            raise TimeoutError("Original observation has not stopped; retain its waiter and API owner before recovery")
        task = self._observation_wait
        self._observation_wait = None
        self._observation_deadline = None
        if not task.cancelled():
            try:
                task.result()
            except ObservedWorkerCrash as notice:
                if notice is not self._observed_crash:
                    raise

    async def _progress(self, phase):
        """Report this phase once; an opted-in consumer can retain delivery uncertainty.

        These labels precede their session invocation, so no condition reporter
        is active. Never apply this policy to an awaited condition: its receipt
        still gates dependent effects. A fresh read confirms lifecycle eligibility,
        not progress acceptance. The synchronous observer owns protected evidence.
        """
        reference = self._record.admission.execution_attempt_ref
        try:
            await self._api(self.api.progress_attempt, reference, phase=phase, condition_code=None)
        except ApiFailure as failure:
            if self._on_unconfirmed_phase_progress is None:
                raise
            async with self._api_lock:
                await self.api.retire(owner=self)
                if not self.api.can_defer_phase_progress(failure, execution_attempt_ref=reference):
                    raise
            self._on_unconfirmed_phase_progress(self._record, phase, failure)
            # Observe outside the exchange lock. Keeping this inside the failed
            # PUT's handler preserves it as context if observation/cleanup fails.
            await self._observe_active()
            if self._api_failure is failure:
                self._api_failure = None


    async def _write_entry(self, record):
        self._checkpoint = EnteredCheckpoint(self._checkpoint, EvidenceDigest.from_evidence(record.prepared_identity))
        self._record = record


    def _terminal(self):
        reference = self._record.admission.execution_attempt_ref
        if type(self._outcome) is AnalysisSuccess:
            body = complete_body(reference, self._outcome.result_schema_id, self._outcome.content)
            operation = TerminalOperation.COMPLETE
        elif type(self._outcome) is AnalysisFailure:
            body = fail_body(reference, self._outcome.code, self._outcome.message)
            operation = TerminalOperation.FAIL
        else:
            raise TypeError("Analysis session returned an unsupported outcome; preserve it for binding diagnosis")
        return DeliveryOutcome(self.scope.api_origin, self.scope.provider_ref,
                               self._record.start.input.job_ref, reference, operation, body)

    async def _handoff(self):
        self._require_live_permission()
        outcome = self._terminal()
        if len(outcome.body) > self.claim.maximum_terminal_bytes:
            raise ExecutionRefused(
                "Cannot hand off this outcome: its encoded request exceeds the reserved report allowance; retain the outcome and cleanup owner"
            )
        self._delivery_outcome = outcome
        self._checkpoint = TerminalSelectedCheckpoint(
            self._checkpoint, self._delivery_outcome.operation,
            self._delivery_outcome.body_sha256, len(self._delivery_outcome.body),
        )
        delay = self._delivery_retry_initial
        while True:
            if self._handoff_stopping.is_set():
                raise ExecutionRefused("Delivery handoff stopped before a new submission; retain the selected outcome for reconciliation.")
            try:
                self._delivery_acceptance = await self.delivery.submit(self._delivery_outcome)
            except (DeliveryUnavailable, DeliveryFull) as failure:
                await self.delivery.retire()
                if (self._handoff_stopping.is_set()
                        or (type(failure) is not DeliveryFull and not self.delivery.can_retry(failure))):
                    raise
                seconds = random.uniform(delay / 2, delay)
                self._notify_delivery_retry(failure, seconds)
                try:
                    await asyncio.wait_for(self._handoff_stopping.wait(), timeout=seconds)
                except TimeoutError:
                    delay = min(self._delivery_retry_max, delay * 2)
                    continue
                raise
            else:
                # The verified relay ACK already transferred the body. A later
                # cleanup failure cannot undo ownership or justify resend.
                self._delivery_outcome = self._outcome = None
                self._refresh_failure()
                self._checkpoint = DeliveryAcceptedCheckpoint(self._checkpoint)
                self.claim.release_terminal(owner=self)
                return

    def _notify_delivery_retry(self, failure, seconds):
        if self._observed_crash is None:
            self._on_delivery_retry(self._record, failure, seconds)
            return
        # Observed-crash reporting must survive a failed logging sink. Other
        # outcomes keep their published fail-stop observer handling.
        # Keep one original sink failure, not an unbounded retry-error history.
        try:
            self._on_delivery_retry(self._record, failure, seconds)
        except Exception as diagnostic:
            if self._delivery_diagnostic_failure is None:
                self._delivery_diagnostic_failure = diagnostic
                self._refresh_failure()

    def _cancel_handoff(self):
        self._handoff_stopping.set()
        if self._handoff_wait is not None and not self._handoff_wait.done() and not self._handoff_wait.cancelling():
            self._handoff_wait.cancel()

    async def _settle_handoff(self):
        if self._handoff_wait is None:
            return
        await asyncio.wait({self._handoff_wait}, timeout=self._task_settle_seconds)
        if not self._handoff_wait.done():
            raise TimeoutError("Original delivery handoff has not settled; retain its bytes and owner without another submission.")
        task = self._handoff_wait
        self._handoff_wait = None
        try:
            task.result()
        except asyncio.CancelledError as interruption:
            self._handoff_interruption = interruption
            self._refresh_failure()
            if self.last_failure is None:
                raise

    async def _retire_delivery(self):
        if self._handoff_wait is not None:
            raise ExecutionRefused("Settle the original handoff task before retiring its delivery transport.")
        await self.delivery.retire()

    def _begin_retirement(self):
        if self._retirement is not _Retirement.CONFIRMED and self._retirement_task is None:
            self._retirement = _Retirement.UNCONFIRMED
            self._retirement_task = asyncio.create_task(contain_task_exit(retire_session, self.session))
            self._retirement_task.add_done_callback(_observe_task_failure)

    async def _retire_session(self):
        if self._retirement is not _Retirement.CONFIRMED:
            self._begin_retirement()
            try:
                # This owner records cancellation of either the observing caller
                # or the retirement child while retaining the original task.
                await asyncio.wait({self._retirement_task}, timeout=self._task_settle_seconds)
                if not self._retirement_task.done():
                    raise TimeoutError("Original session retirement has not finished; retain it before worker reuse.")
                task = self._retirement_task
                self._retirement_task = None
                task.result()
            except asyncio.CancelledError as cause:
                self._remember(cause, cleanup=True)
                raise
            self._retirement = _Retirement.CONFIRMED

    async def _retire_after_failure(self):
        self._cancel_task()
        self._cancel_observation_wait()
        operations = [self._settle_task, self._settle_observation_wait,
                      self._settle_handoff, self._retire_delivery]
        if self._retirement is _Retirement.NOT_ATTEMPTED or self._retirement_task is not None:
            # Do not retry failed normal retirement while handling its own error.
            operations.insert(0, self._retire_session)
        cancellation = None
        fatal = None
        for operation in operations:
            try:
                await contain_task_exit(operation)
            except asyncio.CancelledError as cause:
                # Retirement records cancellation from its own wait and child.
                # Other cleanup ports transfer their evidence here.
                if operation != self._retire_session:
                    self._remember(cause, cleanup=True)
                cancellation = cause
            except (Exception, BaseExceptionGroup) as cause:
                self._remember(cause, cleanup=True)
                if not isinstance(cause, Exception) and fatal is None:
                    fatal = cause
        if cancellation is not None:
            raise cancellation
        return fatal

    async def _retire_api(self):
        if self._observation_wait is not None:
            raise ExecutionRefused(
                "Cannot retire API while its original observation waiter is unresolved; settle that waiter first"
            )
        if self.api.pending_owner is self:
            await self.api.retire(owner=self)


    async def settle(self):
        """Retire work and observe original I/O; never invoke a prepared handle."""
        if not self._active or self._observing:
            raise ExecutionRefused("No unobserved execution lifecycle is available to settle")
        self._observing = True
        cancellation = None
        fatal = None
        failed = False
        try:
            self._cancel_task()
            self._cancel_observation_wait()
            # Independent cleanup must still run when another owner fails to
            # retire. Each port supplies its own bounded observation contract.
            for operation in (
                self._retire_session,
                self._settle_task,
                self._settle_observation_wait,
                self._settle_handoff,
                self._retire_delivery,
                self._retire_api,
            ):
                try:
                    await contain_task_exit(operation)
                except asyncio.CancelledError as cause:
                    cancellation = cause
                    failed = True
                    if operation != self._retire_session:
                        self._remember(cause, cleanup=True)
                except (Exception, BaseExceptionGroup) as cause:
                    failed = True
                    self._remember(cause, cleanup=True)
                    if not isinstance(cause, Exception) and fatal is None:
                        fatal = cause
            if cancellation is not None:
                self._link_interruption(cancellation)
                raise cancellation
            if fatal is not None:
                raise BaseExceptionGroup("Analysis settlement raised a process-control exception", (fatal, self.last_failure)) from None
            if self._outcome_unretained():
                failed = True
                message = "Observed outcome requires explicit disposition; the claim remains held"
                if (
                    type(self._outcome) in (AnalysisSuccess, AnalysisFailure)
                    and _suppression_reason(self.last_failure.cause) is not None
                ):
                    message = "Returned analysis was not selected for delivery; settle original work before recording its suppression disposition; scientific bytes will not survive restart"
                self._remember(
                    ExecutionRefused(message),
                    cleanup=True,
                )
            if failed:
                raise self.last_failure from None
            # silent-defaults: allow-contract -- Only a retained acceptance checkpoint confers a settled delivery acceptance.
            return self._checkpoint if type(self._checkpoint) is DeliveryAcceptedCheckpoint else None
        finally:
            self._observing = False
            self._refresh_failure()
            self._finish_if_settled()

    async def retain_suppressed_disposition(self):
        """Retain why returned evidence was suppressed, without scientific bytes.

        Settle original tasks and I/O first. Recording this disposition does not
        confirm worker retirement or authorize another calculation.
        """
        if (
            not self._active
            or self._observing
            or self.last_failure is None
            or type(self._outcome) not in (AnalysisSuccess, AnalysisFailure)
            or type(self._checkpoint) not in (AcknowledgedCheckpoint, EnteredCheckpoint)
        ):
            raise ExecutionRefused(
                "Cannot record suppression: this interrupted executor has no unselected returned analysis"
            )
        if (
            self._task is not None
            or self._observation_wait is not None
            or self._handoff_wait is not None
            or self.api.pending_owner is self

        ):
            raise ExecutionRefused("Cannot record suppression while original work or I/O is unresolved; settle it first")
        reason = _suppression_reason(self.last_failure.cause)
        if reason is None:
            raise ExecutionRefused(
                "Cannot suppress this outcome: its failure was not a server stop, failed observation or caller cancellation"
            )
        self._observing = True
        try:
            self._checkpoint = SuppressedCheckpoint(self._checkpoint, reason)
            self._outcome = self._delivery_outcome = None
            return self._checkpoint
        except Exception as cause:
            self._remember(cause, cleanup=True)
            raise self.last_failure from None
        finally:
            self._observing = False
            self._refresh_failure()
            self._finish_if_settled()

    def _outcome_unretained(self):
        return self._outcome is not None

    def _finish_if_settled(self):
        if (
            self.work_settled
            and not self._outcome_unretained()
        ):
            self.claim.finish(self)
            self._active = False

    def _refresh_failure(self):
        if self.last_failure is not None:
            self.last_failure.outcome = self._outcome
            self.last_failure.api_failure = self._api_failure
            self.last_failure.operation_interruption = self._operation_interruption
            self.last_failure.condition_failure = self._condition_failure
            self.last_failure.latest_condition_failure = self._latest_condition_failure
            self.last_failure.condition_receipt = self._condition_receipt
            self.last_failure.delivery_acceptance = self._delivery_acceptance
            self.last_failure.delivery_failure = self.delivery.last_failure
            self.last_failure.delivery_diagnostic_failure = self._delivery_diagnostic_failure
            self.last_failure.handoff_interruption = self._handoff_interruption

    def _interruption_has_evidence(self):
        return (
            self._api_failure is not None
            or self._condition_failure is not None
            or self._latest_condition_failure is not None
            or self._condition_receipt is not None
            or self.delivery.last_failure is not None
            or bool(self.last_failure.cleanup_failures)
            or (self._operation_interruption is not None and self._operation_interruption.__cause__ is not None)
        )

    def _link_interruption(self, interruption):
        """Keep the canonical owned record reachable even if a child settles late."""
        previous = interruption.__cause__
        if previous is self.last_failure:
            return
        retained = self.last_failure.interruption_cause
        if retained is None:
            self.last_failure.interruption_cause = previous
        elif previous is not None and previous is not retained:
            self.last_failure.interruption_cause = BaseExceptionGroup("Analysis interruption observations", (retained, previous))
        interruption.__cause__ = self.last_failure

    # silent-defaults: allow-contract -- The first operation failure starts this record; cleanup appends an independent observation while preserving that original primary.
    def _remember(self, cause, *, cleanup=False):
        if self.last_failure is None:
            # silent-defaults: allow-contract -- A first failure may itself be cleanup; otherwise no independent cleanup failure has been observed.
            cleanup_failures = (cause,) if cleanup else ()
            self.last_failure = ExecutionFailed(
                cause=cause,
                outcome=self._outcome,
                api_failure=self._api_failure,
                attempt=self._record,
                cleanup_failures=cleanup_failures,
            )
        elif cleanup:
            self.last_failure.cleanup_failures = (*self.last_failure.cleanup_failures, cause)
        self._refresh_failure()
