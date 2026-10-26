"""Reconcile compact control with the relay or API, never rerun science.

NMRNet d09b77b docs/notes/021_compose_runner_architecture_20261023.html
removes file-body replay from the producer. Existing START
and API receipt owners remain authoritative. The caller confirms native cleanup
before invoking this stopped recovery owner; an API terminal observation closes
only lifecycle uncertainty and never claims the chosen result was published.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import dataclass

from provider_core.api import ApiFailure
from provider_core.attempt_record import StartPending
from provider_core.attempt_response import AttemptObservation
from provider_core.producer_checkpoint import (
    AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint, TerminalSelectedCheckpoint,
    checkpoint_acknowledgement, checkpoint_start,
)
from provider_core.starting import AttemptStarter


class RecoveryRefused(RuntimeError):
    """No replacement science or publication was authorized by this observation."""


class RecoveryFailed(RuntimeError):
    """Protected original observation, transport and independent settlement causes."""

    # silent-defaults: allow-contract -- Recovery can fail before any observation or independent settlement failure exists.
    def __init__(self, cause, cleanup_failures=(), *, api_failure=None, observed_record=None):
        super().__init__("Cannot reconcile retained Attempt control; preserve its identity and settle original I/O without repeating science.")
        self.cause, self.cleanup_failures = cause, cleanup_failures
        self.api_failure, self.observed_record = api_failure, observed_record


@dataclass(frozen=True, slots=True)
class ObservedAttempt:
    """Historical control plus current lifecycle observation, not result acceptance."""

    record: object
    observation: AttemptObservation


class AttemptRecovery:
    """One stopped scoped checkpoint under an existing claim and cleanup proof."""

    def __init__(self, api, store, claim, *, delivery):
        if (api.origin != store.scope.api_origin
                or (delivery.endpoint.api_origin, delivery.endpoint.provider_ref)
                != (store.scope.api_origin, store.scope.provider_ref)):
            raise ValueError("Recovery requires the configured API, checkpoint scope and scoped relay client.")
        self.api, self.store, self.claim, self.delivery = api, store, claim, delivery
        self._used = self._active = self._observing = False
        self._child = self._failure = self._observed = None

    @property
    def last_failure(self):
        return self._child.last_failure if self._child is not None else self._failure

    async def recover(self, record):
        start = checkpoint_start(record)
        if self._used or start.scope != self.store.scope:
            raise RecoveryRefused("Recovery requires an unused owner and the explicitly opened checkpoint scope.")
        self.claim.require_start(start)
        if type(record) is StartPending:
            self._child = AttemptStarter(self.api, self.store, self.claim)
            self._used = True
            return await self._child.resume(record)
        acknowledged = checkpoint_acknowledgement(record)
        self.claim.require(start.scope, acknowledged.admission.execution_attempt_ref)
        self.claim.begin(self)
        self._used = self._active = self._observing = True
        try:
            if record not in await self.store.inventory(owner=self):
                raise RecoveryRefused("Selected checkpoint is no longer current; reread control before recovery.")
            if type(record) is AcknowledgedCheckpoint and record.admission.state != "in_progress":
                await self.store.clear_terminal_start_after_cleanup(record, owner=self)
                return record
            if type(record) is TerminalSelectedCheckpoint:
                row = await self.delivery.status(acknowledged.admission.execution_attempt_ref)
                if row is not None:
                    if (row["job_ref"] != start.input.job_ref or row["operation"] != record.operation.value
                            or row["body_sha256"] != record.body_sha256 or row["body_bytes"] != record.body_bytes):
                        raise RecoveryRefused("The relay retains a different terminal identity; do not replace or repeat either choice.")
                    acceptance = {
                        "api_origin": self.delivery.endpoint.api_origin,
                        "provider_ref": self.delivery.endpoint.provider_ref,
                        **{key: row[key] for key in ("job_ref", "execution_attempt_ref", "operation", "body_sha256")},
                    }
                    record = await self.store.accept_delivery(record, acceptance, owner=self)
            if type(record) is DeliveryAcceptedCheckpoint:
                await self.store.clear_after_cleanup(record, owner=self)
                return record
            observation = await self.api.read_attempt(
                acknowledged.admission.execution_attempt_ref, job_ref=start.input.job_ref, owner=self,
            )
            self._observed = ObservedAttempt(record, observation)
            if observation.state == "in_progress":
                raise RecoveryRefused("Interrupted Attempt is still active without a verified relay handoff; do not repeat scientific execution.")
            await self.store.clear_observed_after_cleanup(record, observation, owner=self)
            return self._observed
        except (Exception, asyncio.CancelledError) as cause:
            self._failure = RecoveryFailed(cause, api_failure=self.api.last_failure, observed_record=self._observed)
            if isinstance(cause, asyncio.CancelledError):
                raise
            raise self._failure from None
        finally:
            self._observing = False
            self._finish_if_settled()

    async def settle(self):
        """Observe original recovery operations; create no replacement commands."""
        if self._child is not None:
            return await self._child.settle()
        if not self._active or self._observing:
            raise RecoveryRefused("No unobserved recovery operation is available to settle.")
        self._observing = True
        failures = []
        try:
            for operation in (self._retire_api, self.delivery.retire, self._settle_storage):
                try:
                    await operation()
                except (Exception, asyncio.CancelledError) as cause:
                    failures.append(cause)
            if failures:
                original = self._failure
                self._failure = RecoveryFailed(original.cause, (*original.cleanup_failures, *failures),
                    api_failure=original.api_failure, observed_record=self._observed)
                for cause in failures:
                    if isinstance(cause, asyncio.CancelledError):
                        raise cause
                raise self._failure from None
        finally:
            self._observing = False
            self._finish_if_settled()

    async def _retire_api(self):
        if self.api.pending_owner is self:
            await self.api.retire(owner=self)

    async def _settle_storage(self):
        if self.store.pending_owner is self:
            await self.store.settle(owner=self)

    def _finish_if_settled(self):
        if (self.api.pending_owner is not self and self.store.pending_owner is not self
                and self.delivery.retirement_confirmed):
            self.claim.finish(self)
            self._active = False
