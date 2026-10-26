"""Own one selected session and its claim across the existing lifecycle owners.

The sequence exercised at NMRNet 5629cb031b54afefbfff2a34d81602c6bc2b19ae,
tests/integration/provider_core/test_live_worker.py::LiveWorkerTests.run_lifecycle, becomes runtime composition
here. Scientific preparation, durable entry and retirement retain their existing
owners. Optional START recovery repeats the exact durable command before preparation.
The executor transfers an approved outcome to the scoped relay independently of
native retirement; this controller clears its checkpoint only after both complete.
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
from provider_core.attempt_record import AcknowledgedAttempt, StartPending
from provider_core.delivery_worker import require_delivery_retry_settings
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint
from provider_core.execution import (
    AttemptExecutor,
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


@dataclass(frozen=True, slots=True)
class StartRecovery:
    """Pace reviewed exact START retries before scientific preparation.

    Individual exchanges and settlement remain bounded; an availability outage
    does not expire the durable command. Protected synchronous observers
    receive the original pending Job before retry and its durable admission after
    recovery; no Attempt reference is assumed before a verified acknowledgement.
    """

    backoff_max_seconds: float
    on_retry: Callable[[StartPending, ApiFailure, float], None] = field(repr=False)
    on_recovered: Callable[[AcknowledgedAttempt, ApiFailure], None] = field(repr=False)

    def __post_init__(self):
        value = self.backoff_max_seconds
        if not (type(value) in (int, float) and math.isfinite(value) and 0 < value):
            raise ValueError("START recovery backoff maximum must be finite positive seconds")
        if not callable(self.on_retry) or not callable(self.on_recovered):
            raise ValueError("START recovery requires explicit protected retry and recovery observers")


class _Lifetime(Enum):
    NEW = auto()
    RUNNING = auto()
    HELD = auto()
    SETTLING = auto()
    RELEASED = auto()


class AttemptController:
    """Accept one session after configuration validation, including pre-start cleanup.

    Composition supplies the shared claim registry and already opened scoped
    store, and retains API/store lifetime. The accepted session belongs here even
    when a start fails or returns a terminal replay. Constructor refusal leaves
    it with the caller. A held controller never starts or publishes fresh work.

    `operation` retains the actual core owner for protected diagnosis and explicit
    executor disposition. Settlement observes outstanding work, including an already
    approved handoff, and requests session retirement when needed. It cannot start
    science after a late admission or select a late scientific outcome for delivery.
    """

    # Optional policies keep fail-stop observation, phase progress, conditions and START unless selected; delivery retry settings and its observer are always explicit.
    def __init__(
        self,
        api,
        store,
        claims,
        session,
        *,
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
        start_recovery=None,
    ):
        if api.origin != store.scope.api_origin:
            raise ValueError("Cannot configure Attempt controller: signing origin differs from retained storage scope")
        require_execution_settings(
            allowed_job_states=allowed_job_states, poll_seconds=poll_seconds, task_settle_seconds=task_settle_seconds
        )
        require_delivery_retry_settings(delivery_retry_initial_seconds, delivery_retry_max_seconds)
        if ((delivery.endpoint.api_origin, delivery.endpoint.provider_ref)
                != (store.scope.api_origin, store.scope.provider_ref) or not callable(on_delivery_retry)):
            raise ValueError("Attempt controller requires its scoped delivery client and protected retry observer.")
        require_observation_recovery(observation_recovery)
        require_phase_progress_observer(on_unconfirmed_phase_progress)
        require_condition_recovery(condition_recovery)
        if start_recovery is not None and type(start_recovery) is not StartRecovery:
            raise ValueError("START recovery must be an admitted policy or explicit fail-stop None")
        self.delivery, self._on_delivery_retry = delivery, on_delivery_retry
        self._delivery_retry_initial = delivery_retry_initial_seconds
        self._delivery_retry_max = delivery_retry_max_seconds
        self._observation_recovery = observation_recovery
        self._on_unconfirmed_phase_progress = on_unconfirmed_phase_progress
        self._condition_recovery = condition_recovery
        self._start_recovery = start_recovery
        self.api, self.store, self._claims, self.session = api, store, claims, session
        self._allowed_job_states = allowed_job_states
        self._poll_seconds, self._task_settle_seconds = poll_seconds, task_settle_seconds
        self._lifetime = _Lifetime.NEW
        self._claim = None
        self._operation = None
        self._retired_by_controller = False
        self.last_failure = None

    @property
    def operation(self):
        """Return the retained start, execution or session owner.

        The object remains available for diagnosis after settlement; its presence
        does not mean work is still running or that the claim is still borrowed.
        """
        return self._operation

    async def run(self, pending):
        if self._lifetime is not _Lifetime.NEW:
            raise ControllerRefused(
                "This controller accepts only one run; retain its original invocation and controller for settlement or recovery"
            )
        self._lifetime = _Lifetime.RUNNING
        try:
            result = await self._run_effects(pending)
            self._claim.release()
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
        inventory = await self.store.inventory(owner=self)
        if inventory:
            raise ControllerRefused("Retained work requires explicit recovery before this session can start a fresh Attempt")
        self._claim = self._claims.claim_start(pending)
        self._operation = AttemptStarter(self.api, self.store, self._claim)
        acknowledged = await self._start_attempt(pending)
        if acknowledged.admission.state != "in_progress":
            self._operation = self.session
            await self._retire_session()
            await self.store.clear_terminal_start_after_cleanup(
                AcknowledgedCheckpoint(acknowledged.start, acknowledged.admission), owner=self,
            )
            return acknowledged
        self._operation = AttemptExecutor(
            self.api,
            self.store,
            self._claim,
            self.session,
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
        accepted = await self._operation.run(acknowledged)
        # Handoff and native retirement are independent, both confirmed here.
        # Clearing control metadata neither publishes nor repeats science.
        await self.store.clear_after_cleanup(accepted, owner=self)
        return accepted

    async def _start_attempt(self, pending):
        policy = self._start_recovery
        first_failure = None
        # silent-defaults: allow-contract -- A missing admitted recovery policy supplies no retry backoff.
        delay = min(self._poll_seconds, policy.backoff_max_seconds) if policy is not None else None
        send = self._operation.start
        while True:
            try:
                acknowledged = await send(pending)
            except StartFailed as failure:
                if (
                    type(failure) is not StartFailed
                    or policy is None
                    or failure.cause is not failure.api_failure
                    or failure.api_failure is None
                    or failure.acknowledgement is not None
                    or failure.settlement_failures
                ):
                    raise
                # Settlement belongs to this same starter. Late transport or
                # independent cleanup evidence must be admitted before replay.
                if self._claim.operation_owner is self._operation:
                    await self._operation.settle()
                if not self.api.can_replay_start(failure.api_failure):
                    raise
                if first_failure is None:
                    first_failure = failure.api_failure
                seconds = random.uniform(delay / 2, delay)
                policy.on_retry(pending, failure.api_failure, seconds)
                await asyncio.sleep(seconds)
                delay = min(policy.backoff_max_seconds, delay * 2)
                send = self._operation.resume
            else:
                if first_failure is not None:
                    policy.on_recovered(acknowledged, first_failure)
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
            # silent-defaults: allow-contract -- A refused admission has no claim; session and inventory cleanup still run without inventing an operation owner.
            owner = self._claim.operation_owner if self._claim is not None else None
            if isinstance(owner, AttemptExecutor):
                operations = (owner.settle, self._settle_inventory)
            else:
                operations = (self._retire_session, self._settle_operation, self._settle_inventory)
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
            if type(result) is DeliveryAcceptedCheckpoint:
                await self.store.clear_after_cleanup(result, owner=self)
            if self._claim is not None:
                self._claim.release()
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
        # silent-defaults: allow-contract -- Pre-claim admission failures leave no borrowed operation; their inventory and session settle through separate owners.
        owner = self._claim.operation_owner if self._claim is not None else None
        if owner is not None:
            return await owner.settle()
        return None

    async def _settle_inventory(self):
        if self.store.pending_owner is self:
            await self.store.settle(owner=self)

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
