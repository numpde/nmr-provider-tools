"""Exclude overlapping owners of a remote Attempt within one event loop.

Precedent: magnet-deploy c5813ee35cfbb1c98a5cc5d20a94787271f26486,
magnet_provider/provider_attempt_recovery.py::ProviderAttemptOwnership. Composition shares one
registry across lanes/recovery. Unlike automatic context-manager release, an
unfinished operation pins its exact handle until its owner confirms settlement.
This is local exclusion, not a distributed lease or proof of worker retirement.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import json

from provider_core.attempt_record import RecoveryScope, StartPending
from provider_core.producer_checkpoint import checkpoint_acknowledgement, checkpoint_start
from provider_core.attempt_response import require_attempt_ref


class OwnershipRefused(RuntimeError):
    """No claim or operation was acquired; existing ownership remains unchanged."""


def _identity(scope, reference):
    if type(scope) is not RecoveryScope:
        raise ValueError("Attempt ownership requires an admitted recovery scope")
    require_attempt_ref(reference)
    # Lane/generation labels cannot partition ownership of one remote Attempt.
    return scope.api_origin, scope.provider_ref, "attempt", reference


def _start_identity(pending):
    if type(pending) is not StartPending:
        raise ValueError("Start ownership requires an admitted pending-start record")
    # nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
    # nmr_api/operations/execution_attempt_start.py:396-412 reserves keys
    # provider-wide: another Job or lane must not form a separate local owner.
    key = json.loads(pending.body)["provider_attempt_key"]
    return pending.scope.api_origin, pending.scope.provider_ref, "start", key


class AttemptClaims:
    def __init__(self):
        self._claims = {}
        self._loop = None

    def claim(self, scope, execution_attempt_ref):
        return self._claim(_identity(scope, execution_attempt_ref))

    def claim_start(self, pending):
        return self._claim(_start_identity(pending))

    def claim_record(self, record):
        """Reserve every known identity, or leave the registry unchanged."""
        return self.claim_records((record,))[0]

    def claim_records(self, records):
        """Reserve a selected recovery inventory before dispatching any replay.

        These records come from the composition's explicitly scoped store. Claims
        exclude competing owners; they do not certify freshness or worker exit.
        """
        groups = []
        for record in records:
            start = checkpoint_start(record)
            identities = [_start_identity(start)]
            if type(record) is not StartPending:
                acknowledged = checkpoint_acknowledgement(record)
                identities.append(_identity(start.scope, acknowledged.admission.execution_attempt_ref))
            groups.append(identities)
        return self._reserve(groups)

    def _claim(self, identity):
        return self._reserve(((identity,),))[0]

    def _reserve(self, groups):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise OwnershipRefused("Attempt claims must remain on their owning event loop")
        self._loop = loop
        selected = set()
        for identities in groups:
            for identity in identities:
                if identity in self._claims or identity in selected:
                    raise OwnershipRefused(
                        "An Attempt identity is already owned or repeated; no selected recovery claims were acquired"
                    )
                selected.add(identity)
        # repo-check: allow-tuple-generator -- Return fixed claims only after every selected identity is admitted.
        claims = tuple(_AttemptClaim(self, identities) for identities in groups)
        for claim in claims:
            for identity in claim._identities:
                self._claims[identity] = claim
        return claims

    def _require(self, claim):
        if asyncio.get_running_loop() is not self._loop or not claim.active:
            raise OwnershipRefused("Attempt claim is released, stale or belongs to another event loop")


class _AttemptClaim:
    def __init__(self, registry, identities):
        self._registry = registry
        self._identities = set(identities)
        self._operation_owner = None

    @property
    def active(self):
        return all(self._registry._claims.get(identity) is self for identity in self._identities)

    @property
    def operation_owner(self):
        """Observe the existing borrower so composition settles that exact owner."""
        self._registry._require(self)
        return self._operation_owner

    def require(self, scope, execution_attempt_ref):
        self._registry._require(self)
        if _identity(scope, execution_attempt_ref) not in self._identities:
            raise OwnershipRefused("Attempt claim differs from the API, provider or Attempt being used")

    def require_start(self, pending):
        self._registry._require(self)
        if _start_identity(pending) not in self._identities:
            raise OwnershipRefused("Start claim differs from the API, provider or retained provider attempt key")

    def bind_attempt(self, scope, execution_attempt_ref, *, owner):
        """Add the received reference atomically without releasing the start key."""
        self._registry._require(self)
        identity = _identity(scope, execution_attempt_ref)
        if self._operation_owner is not owner or owner is None:
            raise OwnershipRefused("Only the current operation owner can bind an Attempt reference")
        if any(known[:2] != identity[:2] or (known[2] == "attempt" and known != identity) for known in self._identities):
            raise OwnershipRefused("Received Attempt differs from the claim's API, provider or previously bound reference")
        existing = self._registry._claims.get(identity)
        if existing is not None and existing is not self:
            raise OwnershipRefused(
                "Received Attempt already has another local owner; preserve the acknowledgement for reconciliation"
            )
        self._registry._claims[identity] = self
        self._identities.add(identity)

    def begin(self, owner):
        self._registry._require(self)
        if owner is None:
            raise ValueError("Attempt operation requires an explicit owner")
        if self._operation_owner is not None:
            raise OwnershipRefused("Attempt claim still owns an operation; settle it before another effect")
        self._operation_owner = owner

    def finish(self, owner):
        self._registry._require(self)
        if self._operation_owner is not owner:
            raise OwnershipRefused("Only the exact operation owner can settle this Attempt claim")
        self._operation_owner = None

    def release(self):
        self._registry._require(self)
        if self._operation_owner is not None:
            raise OwnershipRefused("Cannot release Attempt claim while an operation remains unresolved")
        for identity in self._identities:
            del self._registry._claims[identity]
