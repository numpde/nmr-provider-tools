"""Own one selected session and its claim across the existing lifecycle owners.

The sequence exercised at NMRNet 5629cb031b54afefbfff2a34d81602c6bc2b19ae,
tests/integration/provider_core/test_live_worker.py::LiveWorkerTests.run_lifecycle, becomes runtime composition
here. Scientific preparation, live control and retirement retain their existing
owners. Bounded START retries retain the exact original command before preparation.
The executor transfers an approved outcome to the scoped relay independently of
native retirement; this controller releases its claim only after both complete.
This owner never reruns a session or publishes directly to the API.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto

from provider_core.task_boundary import contain_task_exit
from provider_core.analysis import AnalysisOutcomeUnresolved, retire_session
from provider_core.api import ApiFailure
from provider_core.attempt_record import AcknowledgedAttempt, RecoveryScope, StartPending
from provider_core.delivery_worker import require_delivery_retry_settings
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint, SuppressedCheckpoint
from provider_core.job_input import VerifiedJobInput
from provider_core.execution import (
    AttemptExecutor,
    ExecutionFailed,
    ExecutionStopRequested,
    ObservationGapExpired,
    _observe_task_failure,
    require_condition_recovery,
    require_execution_settings,
    require_observation_recovery,
    require_phase_progress_observer,
)
from provider_core.starting import AttemptStarter, StartFailed
from provider_core.ownership import OwnershipRefused
from provider_core.uploads import UploadRecoveryExhausted


class ControllerRefused(RuntimeError):
    """This invocation dispatched no new start or scientific work."""


class ControllerFailed(RuntimeError):
    """Safe lifecycle context with protected primary and independent cleanup causes."""

    def __init__(self, cause, cleanup_failures):
        super().__init__(
            "The Attempt lifecycle did not finish; retain this controller and its records, then settle original work before recovery."
        )
        self.cause = cause
        self.cleanup_failures = cleanup_failures
        self.interruption_cause = None


class _Lifetime(Enum):
    NEW = auto()
    RUNNING = auto()
    HELD = auto()
    SETTLING = auto()
    RELEASED = auto()


def _create_owned_task(coroutine):
    """Close an unsubmitted coroutine if the event loop refuses its ownership."""
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


@dataclass(frozen=True, slots=True)
class LiveStartPolicy:
    """Pace a bounded live START invocation without forgetting its obligations."""

    cadence_seconds: float
    on_retry: Callable[[StartPending, ApiFailure, float], None] = field(repr=False)
    on_recovered: Callable[[AcknowledgedAttempt, ApiFailure], None] = field(repr=False)
    maximum_replays: int = 1
    window_seconds: float = 90

    def __post_init__(self):
        for name, value in (("cadence_seconds", self.cadence_seconds), ("window_seconds", self.window_seconds)):
            if not (type(value) in (int, float) and math.isfinite(value) and value > 0):
                raise ValueError(f"Live START {name} must be finite positive seconds")
        if type(self.maximum_replays) is not int or not 0 <= self.maximum_replays <= 1:
            raise ValueError("One live START invocation permits zero or one exact replay")
        if not callable(self.on_retry) or not callable(self.on_recovered):
            raise ValueError("Live START requires protected retry and recovery observers")


@dataclass(frozen=True, slots=True)
class StartDeferred:
    """No new START will be sent; uncertainty stays with this exact controller."""

    pending: StartPending = field(repr=False)
    failure: ApiFailure | None = field(repr=False)
    may_have_committed: bool
    acknowledgement: AcknowledgedAttempt | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ActiveStartReplayHeld:
    """The actual active receipt is held without proof of earlier execution."""

    acknowledgement: AcknowledgedAttempt


class AttemptController:
    """Accept one session after configuration validation, including pre-start cleanup.

    Composition supplies the shared live claim registry and retains API lifetime. The accepted session belongs here even
    when a start fails or returns a terminal replay. Constructor refusal leaves
    it with the caller. A held controller never starts or publishes fresh work.

    `operation` retains the actual core owner for protected diagnosis and explicit
    executor disposition. Settlement observes outstanding work, including an already
    approved handoff, and requests session retirement when needed. It cannot start
    science after a late admission or select a late scientific outcome for delivery.
    """


    @classmethod
    # silent-defaults: allow-contract -- Note 010 chooses the 2 MiB report ceiling and refuses unproved active replays; supplied input explicitly replaces the post-START read.
    def live(cls, api, claims, session, *, scope, stopping, start_policy,
             maximum_terminal_bytes=2097152, preverified_input=None, allow_active_start_replay=False, **settings):
        return cls(
            api, scope, claims, session, maximum_terminal_bytes=maximum_terminal_bytes,
            preverified_input=preverified_input, stopping=stopping, start_policy=start_policy,
            allow_active_start_replay=allow_active_start_replay, **settings,
        )

    def __init__(
        self,
        api,
        scope,
        claims,
        session,
        *,
        maximum_terminal_bytes,
        preverified_input,
        stopping,
        start_policy,
        allow_active_start_replay,
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
        admission_stopping=None,
    ):
        claims._require_live_view()
        if type(scope) is not RecoveryScope or api.origin != scope.api_origin:
            raise ValueError("Cannot configure Attempt controller: signing origin differs from the selected scope")
        if preverified_input is not None and type(preverified_input) is not VerifiedJobInput:
            raise ValueError("Attempt controller requires the actual verified Job input or an explicit post-START read")
        if type(stopping) is not asyncio.Event or type(start_policy) is not LiveStartPolicy:
            raise ValueError("Live Attempt controller requires its stop event and admitted LiveStartPolicy")
        if admission_stopping is None:
            admission_stopping = stopping
        if type(admission_stopping) is not asyncio.Event:
            raise ValueError("Live Attempt admission stopping requires an asyncio Event")
        if type(maximum_terminal_bytes) is not int or not 16384 <= maximum_terminal_bytes <= 2097152:
            raise ValueError("Live terminal allowance must cover 16384 through 2097152 encoded request bytes")
        if type(allow_active_start_replay) is not bool:
            raise ValueError("Live Attempt controller requires an explicit boolean active-replay decision")
        require_execution_settings(
            allowed_job_states=allowed_job_states, poll_seconds=poll_seconds, task_settle_seconds=task_settle_seconds
        )
        require_delivery_retry_settings(delivery_retry_initial_seconds, delivery_retry_max_seconds)
        if ((delivery.endpoint.api_origin, delivery.endpoint.provider_ref)
                != (scope.api_origin, scope.provider_ref) or not callable(on_delivery_retry)):
            raise ValueError("Attempt controller requires its scoped delivery client and protected retry observer.")
        require_observation_recovery(observation_recovery)
        require_phase_progress_observer(on_unconfirmed_phase_progress)
        require_condition_recovery(condition_recovery)
        self.delivery, self._on_delivery_retry = delivery, on_delivery_retry
        self._delivery_retry_initial = delivery_retry_initial_seconds
        self._delivery_retry_max = delivery_retry_max_seconds
        self._observation_recovery = observation_recovery
        self._on_unconfirmed_phase_progress = on_unconfirmed_phase_progress
        self._condition_recovery = condition_recovery
        self.api, self._claims, self.session = api, claims, session
        self.scope = scope
        self._maximum_terminal_bytes = maximum_terminal_bytes
        self._preverified_input = preverified_input
        self._stopping, self._start_policy = stopping, start_policy
        self._admission_stopping = admission_stopping
        self._allow_active_start_replay = allow_active_start_replay
        self._allowed_job_states = allowed_job_states
        self._poll_seconds, self._task_settle_seconds = poll_seconds, task_settle_seconds
        self._lifetime = _Lifetime.NEW
        self._driver = None
        self._driver_interruption = None
        self._stop_requested = asyncio.Event()
        self._claim = None
        self._operation = None
        self._acknowledged = None
        self._start_deferred = None
        self._retired_by_controller = False
        self.last_failure = None

    @property
    def operation(self):
        """Return the retained start, execution or session owner.

        The object remains available for diagnosis after settlement; its presence
        does not mean work is still running or that the claim is still borrowed.
        """
        return self._operation

    @property
    def work_settled(self):
        """Confirm cleanup without granting permission to forget a held Job."""
        if (self._lifetime in (_Lifetime.NEW, _Lifetime.RUNNING, _Lifetime.SETTLING)
                or not self.api.retirement_confirmed or not self.delivery.retirement_confirmed
                ):
            return False
        if isinstance(self._operation, AttemptExecutor):
            return self._operation.work_settled
        return self._retired_by_controller and (
            self._claim is None or not self._claim.active or self._claim.operation_owner is None
        )

    @property
    def shutdown_ready(self):
        """Permit ending this lifetime only after cleanup and report duties settle.

        A passive held Job can remain excluded. An exact report without delivery's
        acknowledgement, or a returned outcome needing disposition, cannot vanish
        just because its I/O stopped. Composition must also settle its other owners.
        """
        return self.work_settled and (
            not isinstance(self._operation, AttemptExecutor) or self._operation.shutdown_ready
        )

    def require_held_failure(self, pending, failure):
        """Confirm this selected START's quiescent held failure, without release.

        Return the actual retained failure for protected reporting. Only the
        reviewed scientific, source-exhaustion and observation-gap holds qualify.
        Composition still owns resource readiness, stop/control checks and the
        decision to continue. This check settles, releases and dispatches nothing.
        Bindings must relay original owned errors without changing their evidence;
        this in-process contract does not authenticate an exception's producer.
        """
        executor = self._operation
        if (self._lifetime is not _Lifetime.HELD
                or failure is not self.last_failure
                or type(failure) is not ControllerFailed
                or type(executor) is not AttemptExecutor
                or type(failure.cause) is not ExecutionFailed
                or failure.cause is not executor.last_failure):
            raise ControllerRefused("Cannot confirm a held Attempt: this controller has no matching retained execution failure.") from self.last_failure
        execution = failure.cause
        reason = execution.cause
        if type(reason) not in (AnalysisOutcomeUnresolved, UploadRecoveryExhausted, ObservationGapExpired):
            raise ControllerRefused("Cannot confirm a held Attempt: this failure is not an admitted unresolved analysis, source exhaustion or observation gap.") from failure
        admitted_api_failure = None
        if type(reason) is ObservationGapExpired:
            if reason._executor is not executor or (not reason.read_started and reason.cause is not None):
                raise ControllerRefused("Cannot confirm an observation-gap hold: its original observation does not belong to this executor.") from failure
            admitted_api_failure = reason.cause
        if type(reason) is UploadRecoveryExhausted and (reason.cleanup_failures or reason.interruption is not None):
            raise failure
        if (failure.cleanup_failures or execution.cleanup_failures
                or execution.api_failure is not admitted_api_failure
                or execution.condition_failure is not None
                or execution.latest_condition_failure is not None
                or execution.delivery_failure is not None
                or failure.interruption_cause is not None
                or execution.interruption_cause is not None
                or execution.operation_interruption is not None
                or execution.handoff_interruption is not None
                or not self.shutdown_ready):
            # Positive later retirement does not erase a recorded independent
            # failure. Preserve that original graph rather than manufacture a
            # claim-release refusal and call it a cleanup failure.
            raise failure
        if (type(reason) is ObservationGapExpired and admitted_api_failure is not None
                and not self.api.can_retry_read(admitted_api_failure)):
            # Gap expiry can interrupt read retirement before its recovery loop
            # consults API admission. The original API owner also rejects nested
            # transport cleanup/interruption and non-recoverable read failures.
            raise failure
        claim = self._claim
        if claim is None or executor.claim is not claim:
            raise ControllerRefused("Cannot confirm a held Attempt: its exact lifecycle claim is unavailable.") from failure
        try:
            if claim.lifecycle_owner is not self or claim.operation_owner is not None:
                raise ControllerRefused("Cannot confirm a held Attempt: its exact lifecycle claim is unavailable.") from failure
            # This owning-loop check binds Job, START key and input to THIS
            # active claim; two separate inventory queries cannot prove that link.
            claim.require_start(pending)
            if (type(execution.attempt) is not AcknowledgedAttempt
                    or type(self._acknowledged) is not AcknowledgedAttempt
                    or execution.attempt is not executor._record
                    or execution.attempt.start != pending
                    or execution.attempt.admission is not self._acknowledged.admission):
                raise ControllerRefused("Cannot confirm a held Attempt: its retained admission differs from this selected Attempt.") from failure
            claim.require(self.scope, execution.attempt.admission.execution_attempt_ref)
        except OwnershipRefused as cause:
            raise ControllerRefused("Cannot confirm a held Attempt: the selected START is not held by this controller.") from ExceptionGroup(
                "Retained execution failure and independent ownership refusal", (failure, cause),
            )
        return failure

    async def run(self, pending):
        """Start once and observe the retained driver without transferring ownership.

        Cancelling this wait does not cancel the lifecycle. Use request_stop to
        withdraw future effects, then join before settling its original owners.
        """
        if self._lifetime is not _Lifetime.NEW:
            raise ControllerRefused(
                "This controller accepts only one run; retain its original invocation and controller for settlement or recovery"
            )
        self._lifetime = _Lifetime.RUNNING
        try:
            self._reserve_start(pending)
            self._driver = _create_owned_task(self._drive(pending))
            self._driver.add_done_callback(_observe_task_failure)
        except (Exception, BaseExceptionGroup, asyncio.CancelledError, KeyboardInterrupt, SystemExit) as cause:
            self._remember(cause)
            try:
                cleanup_fatal = await self._retire_after_failure()
            except asyncio.CancelledError as interruption:
                if isinstance(cause, asyncio.CancelledError):
                    self._link_interruption(cause)
                    raise cause from self.last_failure
                self._link_interruption(interruption)
                raise
            finally:
                self._lifetime = _Lifetime.HELD
            if isinstance(cause, asyncio.CancelledError):
                self._link_interruption(cause)
                raise
            if not isinstance(cause, Exception):
                raise BaseExceptionGroup("Attempt admission raised a process-control exception", (cause, self.last_failure)) from None
            if cleanup_fatal is not None:
                raise BaseExceptionGroup("Attempt admission failed during cleanup", (cleanup_fatal, self.last_failure)) from None
            raise self.last_failure from None
        return await self.join()

    async def join(self):
        """Observe the original driver; repeated or cancelled waits dispatch nothing."""
        if self._driver is None:
            raise ControllerRefused("This controller has no original driver to join; no new work was dispatched")
        observer = asyncio.current_task()
        cancellations = observer.cancelling()
        try:
            return await asyncio.shield(self._driver)
        except asyncio.CancelledError:
            if observer.cancelling() == cancellations and self._driver_interruption is not None:
                raise self._driver_interruption
            raise

    def request_stop(self):
        """Withdraw future START/scientific effects without revoking selected delivery."""
        self._stop_requested.set()

    def _stopping_requested(self):
        return self._stop_requested.is_set() or self._stopping.is_set()

    def _admission_stopping_requested(self):
        return self._stopping_requested() or self._admission_stopping.is_set()

    async def _wait_for_admission_stop(self, seconds):
        waits = []
        try:
            events = (self._stop_requested, self._stopping)
            if self._admission_stopping is not self._stopping:
                events += (self._admission_stopping,)
            for event in events:
                waits.append(_create_owned_task(event.wait()))
            done, _ = await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
            return bool(done)
        finally:
            for wait in waits:
                wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def _drive(self, pending):
        try:
            return await contain_task_exit(self._run_lifetime, pending)
        except asyncio.CancelledError as cause:
            # Task observation reconstructs CancelledError; retain the original
            # exception so its independent cleanup evidence is not erased.
            self._driver_interruption = cause
            raise

    async def _run_lifetime(self, pending):
        self._start_deferred = None
        try:
            result = await contain_task_exit(self._run_effects, pending)
            if (type(result) is StartDeferred and result.may_have_committed) or type(result) is ActiveStartReplayHeld:
                return result
            self._claim.release(owner=self)
            self._lifetime = _Lifetime.RELEASED
            return result
        except asyncio.CancelledError as cause:
            self._remember(cause)
            try:
                await self._retire_after_failure()
            except asyncio.CancelledError:
                self._link_interruption(cause)
                raise cause from self.last_failure
            self._link_interruption(cause)
            raise
        except (Exception, BaseExceptionGroup) as cause:
            self._remember(cause)
            try:
                cleanup_fatal = await self._retire_after_failure()
            except asyncio.CancelledError as interruption:
                self._link_interruption(interruption)
                raise
            if not isinstance(cause, Exception):
                raise BaseExceptionGroup("Attempt lifecycle raised a process-control exception", (cause, self.last_failure)) from None
            if cleanup_fatal is not None:
                raise BaseExceptionGroup("Attempt cleanup raised a process-control exception", (cleanup_fatal, self.last_failure)) from None
            raise self.last_failure from None
        finally:
            if self._lifetime is _Lifetime.RUNNING:
                self._lifetime = _Lifetime.HELD

    def _reserve_start(self, pending):
        if type(pending) is not StartPending or pending.scope != self.scope:
            raise ControllerRefused("Live START requires the selected API, provider, lane and generation")
        if self._preverified_input is not None and self._preverified_input.identity != pending.input:
            raise ControllerRefused("Verified input differs from the selected START; no request was sent")
        self._claim = self._claims.claim_live(
            pending, lifecycle_owner=self, maximum_terminal_bytes=self._maximum_terminal_bytes,
        )
        self._defer_start(pending, None, False)
        self._operation = AttemptStarter.live(self.api, self._claim, scope=self.scope)

    async def _run_effects(self, pending):
        acknowledged = await self._start_live_attempt(pending)
        if type(acknowledged) is StartDeferred:
            await self._retire_session()
            return acknowledged
        self._acknowledged = acknowledged
        if acknowledged.admission.state != "in_progress":
            self._operation = self.session
            await self._retire_session()
            return acknowledged
        if acknowledged.admission.replayed and not self._allow_active_start_replay:
            await self._retire_session()
            return ActiveStartReplayHeld(acknowledged)
        self._operation = self._new_executor()
        accepted = await self._operation.run(acknowledged)
        return accepted

    def _new_executor(self):
        settings = dict(
            delivery=self.delivery,
            on_delivery_retry=self._on_delivery_retry,
            delivery_retry_initial_seconds=self._delivery_retry_initial,
            delivery_retry_max_seconds=self._delivery_retry_max,
            allowed_job_states=self._allowed_job_states,
            poll_seconds=self._poll_seconds,
            task_settle_seconds=self._task_settle_seconds,
            observation_recovery=self._observation_recovery,
            on_unconfirmed_phase_progress=self._on_unconfirmed_phase_progress,
            condition_recovery=self._condition_recovery,
        )
        return AttemptExecutor.live(
            self.api, self._claim, self.session, scope=self.scope,
            preverified_input=self._preverified_input, **settings,
        )

    def require_execution_ready(self, claim, executor, acknowledged):
        if (
            self._lifetime is not _Lifetime.RUNNING
            or claim is not self._claim
            or claim.lifecycle_owner is not self
            or executor is not self._operation
            or acknowledged is not self._acknowledged
        ):
            raise ControllerRefused("Cannot begin live execution without this running controller's original admission and executor")
        if self._stopping_requested():
            raise ExecutionStopRequested("Cannot continue science or select a report: the supervising owner requested stopping")
        if acknowledged.admission.replayed and not self._allow_active_start_replay:
            raise ControllerRefused("Cannot execute an active START replay with unproved earlier execution")

    def require_claim_release_ready(self, claim):
        if (
            claim is not self._claim or claim.lifecycle_owner is not self
            or claim.operation_owner is not None or not self.api.retirement_confirmed
            or not self.delivery.retirement_confirmed
        ):
            raise ControllerRefused("Cannot release live ownership while original I/O or borrowers remain")
        if isinstance(self._operation, AttemptExecutor):
            executor = self._operation
            if (
                executor.claim is not claim or executor._active or executor._observing
                or not executor.retirement_confirmed
                or type(executor._checkpoint) not in (DeliveryAcceptedCheckpoint, SuppressedCheckpoint)
            ):
                raise ControllerRefused("Cannot release live ownership before the executor settles cleanup and records a delivery or suppression disposition")
            return
        if not self._retired_by_controller:
            raise ControllerRefused("Cannot release live ownership before this session's cleanup is confirmed")
        if self._start_deferred is not None and not self._start_deferred.may_have_committed:
            return
        if self._acknowledged is not None and self._acknowledged.admission.state != "in_progress":
            return
        raise ControllerRefused("Cannot release this live Job: START may have committed or its active Attempt lacks a valid disposition")

    def _defer_start(self, pending, failure, possible_commit):
        self._start_deferred = StartDeferred(pending, failure, possible_commit, self._acknowledged)
        return self._start_deferred

    async def _start_live_attempt(self, pending):
        policy, loop = self._start_policy, asyncio.get_running_loop()
        if self._admission_stopping_requested():
            return self._defer_start(pending, None, False)
        deadline = loop.time() + policy.window_seconds
        possible_commit = False
        first_failure = latest_failure = None
        replays = 0
        send = self._operation.start
        while True:
            try:
                acknowledged = await send(pending)
            except StartFailed as failure:
                if (
                    type(failure) is not StartFailed or failure.api_failure is None
                    or failure.cause is not failure.api_failure or failure.acknowledgement is not None
                    or failure.settlement_failures
                ):
                    raise
                latest_failure = failure.api_failure
                retry_after = latest_failure.retry_after_seconds
                retry_at = None
                if retry_after is not None:
                    retry_at = loop.time() + retry_after
                possible_commit |= latest_failure.attempt_mutation_may_have_committed
                if self._claim.operation_owner is self._operation:
                    await self._operation.settle()
                possible_commit |= latest_failure.attempt_mutation_may_have_committed
                # Retirement can reveal headers that were unavailable earlier.
                # Start their local floor only when first observed, conservatively.
                if retry_at is None:
                    retry_after = latest_failure.retry_after_seconds
                    if retry_after is not None:
                        retry_at = loop.time() + retry_after
                if (self._admission_stopping_requested() or replays >= policy.maximum_replays
                        or not self.api.can_replay_start(latest_failure)):
                    return self._defer_start(pending, latest_failure, possible_commit)
                if first_failure is None:
                    first_failure = latest_failure
                seconds = max(random.uniform(1, 2), policy.cadence_seconds)
                if retry_at is not None:
                    seconds = max(seconds, retry_at - loop.time())
                allowance = self.api.exchange_seconds + self.api.retirement_seconds
                if seconds + allowance > deadline - loop.time():
                    return self._defer_start(pending, latest_failure, possible_commit)
                policy.on_retry(pending, latest_failure, seconds)
                if self._admission_stopping_requested() or seconds + allowance > deadline - loop.time():
                    return self._defer_start(pending, latest_failure, possible_commit)
                if await self._wait_for_admission_stop(seconds):
                    return self._defer_start(pending, latest_failure, possible_commit)
                if self._admission_stopping_requested() or allowance > deadline - loop.time():
                    return self._defer_start(pending, latest_failure, possible_commit)
                replays += 1
                send = self._operation.resume
            else:
                self._acknowledged = acknowledged
                if first_failure is not None:
                    policy.on_recovered(acknowledged, first_failure)
                if acknowledged.admission.state == "in_progress" and (self._admission_stopping_requested() or loop.time() > deadline):
                    return self._defer_start(pending, latest_failure, True)
                return acknowledged


    async def _retire_after_failure(self):
        # silent-defaults: allow-contract -- Admission can fail before a claim exists; that state has no borrowed operation to settle.
        owner = self._claim.operation_owner if self._claim is not None else None
        if isinstance(owner, AttemptExecutor) or self._operation is self.session:
            return
        try:
            await self._retire_session()
        except asyncio.CancelledError as cause:
            self._remember(cause, cleanup=True)
            raise
        except (Exception, BaseExceptionGroup) as cause:
            self._remember(cause, cleanup=True)
            if not isinstance(cause, Exception):
                return cause
        return None

    async def settle(self):
        """Settle exact owners and release only after confirmed session retirement."""
        if self._lifetime in (_Lifetime.RUNNING, _Lifetime.SETTLING):
            raise ControllerRefused("The controller is already observing its lifetime; await that invocation before settlement")
        if self._lifetime is _Lifetime.RELEASED:
            return None
        self._lifetime = _Lifetime.SETTLING
        result = None
        failures = []
        try:
            # silent-defaults: allow-contract -- A refused admission has no claim; session cleanup still run without inventing an operation owner.
            owner = self._claim.operation_owner if self._claim is not None else None
            if isinstance(owner, AttemptExecutor):
                operations = (owner.settle,)
            else:
                operations = (self._retire_session, self._settle_operation)
            for operation in operations:
                try:
                    value = await contain_task_exit(operation)
                    if value is not None:
                        result = value
                except (Exception, BaseExceptionGroup, asyncio.CancelledError) as cause:
                    failures.append(cause)
                    self._remember(cause, cleanup=True)
            if failures:
                for cause in failures:
                    if isinstance(cause, asyncio.CancelledError):
                        self._link_interruption(cause)
                        raise cause
                for cause in failures:
                    if not isinstance(cause, Exception):
                        raise BaseExceptionGroup("Attempt settlement raised a process-control exception", (cause, self.last_failure)) from None
                raise self.last_failure from None
            try:
                await contain_task_exit(self.delivery.retire)
            except asyncio.CancelledError as cause:
                self._remember(cause, cleanup=True)
                self._link_interruption(cause)
                raise
            except (Exception, BaseExceptionGroup) as cause:
                self._remember(cause, cleanup=True)
                if not isinstance(cause, Exception):
                    raise BaseExceptionGroup("Attempt delivery retirement raised a process-control exception", (cause, self.last_failure)) from None
                raise self.last_failure from None
            if self._claim is not None:
                if (
                    not isinstance(self._operation, AttemptExecutor)
                    and ((self._start_deferred is not None and self._start_deferred.may_have_committed)
                         or (self._acknowledged is not None and self._acknowledged.admission.state == "in_progress"))
                    and self._claim.operation_owner is None
                    and self._retired_by_controller
                    and self.api.retirement_confirmed
                    and self.delivery.retirement_confirmed
                ):
                    return result
                try:
                    self._claim.release(owner=self)
                except ControllerRefused as cause:
                    self._remember(cause, cleanup=True)
                    raise self.last_failure from None
            self._lifetime = _Lifetime.RELEASED
            return result
        finally:
            if self._lifetime is _Lifetime.SETTLING:
                self._lifetime = _Lifetime.HELD

    async def _retire_session(self):
        if self._retired_by_controller:
            return
        if isinstance(self._operation, AttemptExecutor) and self._operation.retirement_confirmed:
            return
        await contain_task_exit(retire_session, self.session)
        # Record only this controller's cleanup confirmation. Execution retains
        # its own confirmation; an approved handoff can finish independently.
        self._retired_by_controller = True

    async def _settle_operation(self):
        # silent-defaults: allow-contract -- Pre-claim admission failures leave no borrowed operation; their session settles through its original owner.
        owner = self._claim.operation_owner if self._claim is not None else None
        if owner is not None:
            return await owner.settle()
        return None


    # silent-defaults: allow-contract -- Ordinary call failures are primary; cleanup callers explicitly add an independent failure while preserving the primary cause.
    def _remember(self, cause, *, cleanup=False):
        if self.last_failure is None:
            self.last_failure = ControllerFailed(cause, ())
        elif cleanup:
            self.last_failure.cleanup_failures = (*self.last_failure.cleanup_failures, cause)

    def _link_interruption(self, interruption):
        """Keep original interruption evidence and this owner's current cleanup."""
        previous = interruption.__cause__
        if previous is self.last_failure:
            return
        retained = self.last_failure.interruption_cause
        if retained is None:
            self.last_failure.interruption_cause = previous
        elif previous is not None and previous is not retained:
            self.last_failure.interruption_cause = BaseExceptionGroup("Attempt interruption observations", (retained, previous))
        interruption.__cause__ = self.last_failure
