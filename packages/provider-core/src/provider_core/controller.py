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

from provider_core.analysis import retire_session
from provider_core.api import ApiFailure
from provider_core.attempt_record import AcknowledgedAttempt, RecoveryScope, StartPending
from provider_core.delivery_worker import require_delivery_retry_settings
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint, SuppressedCheckpoint
from provider_core.job_input import VerifiedJobInput
from provider_core.execution import (
    AttemptExecutor,
    ExecutionStopRequested,
    require_condition_recovery,
    require_execution_settings,
    require_observation_recovery,
    require_phase_progress_observer,
)
from provider_core.starting import AttemptStarter, StartFailed


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


class _Lifetime(Enum):
    NEW = auto()
    RUNNING = auto()
    HELD = auto()
    SETTLING = auto()
    RELEASED = auto()


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
        claims._require_live_view()
        controller = cls.__new__(cls)
        controller._configure(
            api, scope, claims, session, maximum_terminal_bytes=maximum_terminal_bytes,
            preverified_input=preverified_input, stopping=stopping, start_policy=start_policy,
            allow_active_start_replay=allow_active_start_replay, **settings,
        )
        return controller

    def _configure(
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
    ):
        if type(scope) is not RecoveryScope or api.origin != scope.api_origin:
            raise ValueError("Cannot configure Attempt controller: signing origin differs from the selected scope")
        if preverified_input is not None and type(preverified_input) is not VerifiedJobInput:
            raise ValueError("Attempt controller requires the actual verified Job input or an explicit post-START read")
        if type(stopping) is not asyncio.Event or type(start_policy) is not LiveStartPolicy:
            raise ValueError("Live Attempt controller requires its stop event and admitted LiveStartPolicy")
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
        self._allow_active_start_replay = allow_active_start_replay
        self._allowed_job_states = allowed_job_states
        self._poll_seconds, self._task_settle_seconds = poll_seconds, task_settle_seconds
        self._lifetime = _Lifetime.NEW
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

    async def run(self, pending):
        if self._lifetime is not _Lifetime.NEW:
            raise ControllerRefused(
                "This controller accepts only one run; retain its original invocation and controller for settlement or recovery"
            )
        self._lifetime = _Lifetime.RUNNING
        try:
            result = await self._run_effects(pending)
            if (type(result) is StartDeferred and result.may_have_committed) or type(result) is ActiveStartReplayHeld:
                return result
            self._claim.release(owner=self)
            self._lifetime = _Lifetime.RELEASED
            return result
        except asyncio.CancelledError as cause:
            self._remember(cause)
            await self._retire_after_failure()
            raise
        except Exception as cause:
            self._remember(cause)
            await self._retire_after_failure()
            raise self.last_failure from None
        finally:
            if self._lifetime is _Lifetime.RUNNING:
                self._lifetime = _Lifetime.HELD

    async def _run_effects(self, pending):
        if type(pending) is not StartPending or pending.scope != self.scope:
            raise ControllerRefused("Live START requires the selected API, provider, lane and generation")
        if self._preverified_input is not None and self._preverified_input.identity != pending.input:
            raise ControllerRefused("Verified input differs from the selected START; no request was sent")
        self._claim = self._claims.claim_live(
            pending, lifecycle_owner=self, maximum_terminal_bytes=self._maximum_terminal_bytes,
        )
        self._operation = AttemptStarter.live(self.api, self._claim, scope=self.scope)
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
        if self._stopping.is_set():
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
        if self._stopping.is_set():
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
                if (self._stopping.is_set() or replays >= policy.maximum_replays
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
                if self._stopping.is_set() or seconds + allowance > deadline - loop.time():
                    return self._defer_start(pending, latest_failure, possible_commit)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
                except TimeoutError:
                    # This wait owns only the pacing timer, never API/native I/O.
                    if self._stopping.is_set() or allowance > deadline - loop.time():
                        return self._defer_start(pending, latest_failure, possible_commit)
                else:
                    return self._defer_start(pending, latest_failure, possible_commit)
                replays += 1
                send = self._operation.resume
            else:
                self._acknowledged = acknowledged
                if first_failure is not None:
                    policy.on_recovered(acknowledged, first_failure)
                if acknowledged.admission.state == "in_progress" and (self._stopping.is_set() or loop.time() > deadline):
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
        except Exception as cause:
            self._remember(cause, cleanup=True)

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
                    value = await operation()
                    if value is not None:
                        result = value
                except (Exception, asyncio.CancelledError) as cause:
                    failures.append(cause)
                    self._remember(cause, cleanup=True)
            if failures:
                for cause in failures:
                    if isinstance(cause, asyncio.CancelledError):
                        raise cause
                raise self.last_failure from None
            await self.delivery.retire()
            if self._claim is not None:
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
        await retire_session(self.session)
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
        original = self.last_failure
        cleanup_failures = ()
        if original is not None and cleanup:
            cleanup_failures = (*original.cleanup_failures, cause)
        self.last_failure = ControllerFailed(
            original.cause if original is not None else cause,
            cleanup_failures,
        )
