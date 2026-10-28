"""Own exact start admission and explicit replay before scientific preparation.

NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_lifecycle.py:442-508 admits the start before sending;
:648-695 binds acknowledgement before continuing; :1636-1664 checks generation
before recovery.
Here terminal replays also remain retained. No preparation or execution is granted.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio

from provider_core.api import ApiFailure
from provider_core.attempt_record import RecoveryScope, StartPending, acknowledge_start
from provider_core.ownership import OwnershipRefused
from provider_core.transport import ExchangeFailed


class StartRefused(RuntimeError):
    """This call dispatched no start request."""


class StartFailed(RuntimeError):
    """Safe context with protected original causes and an unretained admission."""

    def __init__(self, message, *, cause, acknowledgement, api_failure, settlement_failures):
        super().__init__(message)
        self.cause = cause
        self.acknowledgement = acknowledgement
        self.api_failure = api_failure
        self.settlement_failures = settlement_failures


class AttemptStarter:
    """Borrow one start claim until its actual acknowledgement is retained.

    The caller owns the claim after return, including terminal replay admissions.
    The bounded selection and received acknowledgement remain with their live owner.
    Successful acknowledgement says nothing about preparation or worker retirement.
    """


    @classmethod
    def live(cls, api, claim, *, scope):
        return cls(api, scope, claim)

    def __init__(self, api, scope, claim):
        claim.require_live(scope)
        if type(scope) is not RecoveryScope or api.origin != scope.api_origin:
            raise ValueError("Cannot configure Attempt start: signing origin differs from the selected scope")
        self.api = api
        self.scope = scope
        self.claim = claim
        self._pending = None
        self._unretained = None
        self._api_failure = None
        self._observing = False
        self.last_failure = None


    async def start(self, pending):
        """Retain a new pending start, or the identical existing one, before sending."""
        return await self._start(pending)

    async def resume(self, pending):
        """Replay only an exact existing pending start under the current scope."""
        return await self._start(pending)

    async def _start(self, pending):
        if type(pending) is not StartPending or pending.scope != self.scope:
            raise StartRefused("Start requires a pending record matching the current API, provider, lane and generation")
        if self._pending is not None:
            raise StartRefused("Attempt start still owns unresolved work; settle it before another request")
        self.claim.require_live_start(pending)
        self.claim.begin(self)
        self._pending = pending
        self._observing = True
        self._api_failure = None
        self.last_failure = None
        try:
            self._unretained = await self._send(pending)
            self.claim.bind_attempt(self.scope, self._unretained.admission.execution_attempt_ref, owner=self)
            # Return the actual received admission. Historical disk metadata is
            # deliberately a different type and cannot recreate this live path.
            result = self._unretained
            self._unretained = None
            return result
        except asyncio.CancelledError as cause:
            self._remember("Attempt start was cancelled; preserve its exact request and settle owned I/O before reuse", cause)
            raise
        except (ApiFailure, OwnershipRefused) as cause:
            raise self._remember(
                "Attempt start lacks a retained owned acknowledgement; preserve the original request and protected admission evidence",
                cause,
            ) from None
        finally:
            self._observing = False
            self._finish_if_settled()

    async def _send(self, pending):
        try:
            admission = await self.api.start_attempt(
                pending.body,
                job_ref=pending.input.job_ref,
                analysis_kind_ref=pending.analysis_kind_ref,
                provider_ref=pending.scope.provider_ref,
                owner=self,
            )
        except asyncio.CancelledError:
            self._api_failure = self.api.last_failure
            raise
        except ApiFailure as failure:
            self._api_failure = failure
            raise
        return acknowledge_start(pending, admission)

    async def settle(self):
        """Settle original I/O without inventing a late acknowledgement."""
        if self._pending is None or self._observing:
            raise StartRefused("No unobserved start operation is available to settle")
        self._observing = True
        try:
            if self.api.pending_owner is self:
                await self.api.retire(owner=self)
            if self._unretained is not None:
                raise StartRefused(
                    "Received acknowledgement remains unresolved; retain its owner and reconcile the claim before preparation"
                )
            return None
        except asyncio.CancelledError as cause:
            self._remember("Start settlement was cancelled; original ownership remains retained", cause, settlement=True)
            raise
        except (StartRefused, ExchangeFailed) as cause:
            raise self._remember(
                "Cannot settle Attempt start; preserve primary evidence and the start claim", cause, settlement=True
            ) from None
        finally:
            self._observing = False
            self._finish_if_settled()

    def _finish_if_settled(self):
        if self.api.pending_owner is not self and self._unretained is None:
            self.claim.finish(self)
            self._pending = None

    # silent-defaults: allow-contract -- Ordinary failure records the primary; settlement=True appends separately observed settlement evidence.
    def _remember(self, message, cause, *, settlement=False):
        # silent-defaults: allow-contract -- Absent earlier settlement evidence remains absent.
        original = self.last_failure if settlement else None
        # silent-defaults: allow-contract -- Retain the original primary and append only observed settlement failures; the empty tuple means none.
        failure = StartFailed(
            message,
            cause=original.cause if original is not None else cause,
            acknowledgement=self._unretained,
            api_failure=self._api_failure,
            settlement_failures=(*original.settlement_failures, cause)
            if original is not None
            else ((cause,) if settlement else ()),
        )
        self.last_failure = failure
        return failure
