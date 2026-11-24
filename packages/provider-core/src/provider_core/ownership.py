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
import reprlib
from dataclasses import dataclass

from provider_core.adapters.nmr.v1.attempt_request import retained_start_key
from provider_core.attempt_record import RecoveryScope, StartPending, _encode_pending
from provider_core.validation.upstream.nmr.v1.attempt_response import require_attempt_ref
from provider_core.validation.upstream.nmr.v1.attempt_request import MAX_COMPLETE_REQUEST_BYTES
from provider_core.validation.upstream.nmr.v1.job_refs import require_job_ref
from provider_core.tools import metadata_mismatch_message
from provider_core.type_requirements import require_type


# Keep ordinary routing identities complete while bounding rejected scope text
# repo-check: allow-module-constant
_OWNERSHIP_EVIDENCE = reprlib.Repr()

_OWNERSHIP_EVIDENCE.maxstring = 256


class OwnershipRefused(RuntimeError):
    """No claim or operation was acquired; existing ownership remains unchanged."""


class LiveCapacityFull(OwnershipRefused):
    """This selection acquired no claim; settle owners or wait for capacity."""


@dataclass(frozen=True, slots=True)
class LiveAttemptLimits:
    maximum_attempts: int = 8
    maximum_control_bytes: int = 1048576
    maximum_pending_terminal_bytes: int = 16777216
    maximum_input_bytes: int = 16777216

    def __post_init__(self):
        require_type(self.maximum_attempts, int)

        if not (0 < self.maximum_attempts):
            raise ValueError(f"{self.maximum_attempts=} should be positive")

        require_type(self.maximum_control_bytes, int)

        if not (0 < self.maximum_control_bytes):
            raise ValueError(f"{self.maximum_control_bytes=} should be positive")

        require_type(self.maximum_pending_terminal_bytes, int)

        if not (0 < self.maximum_pending_terminal_bytes):
            raise ValueError(f"{self.maximum_pending_terminal_bytes=} should be positive")

        require_type(self.maximum_input_bytes, int)

        if not (0 < self.maximum_input_bytes):
            raise ValueError(f"Cannot bound admitted Job inputs: {self.maximum_input_bytes=} should be positive")


def _identity(scope, reference):
    require_type(scope, RecoveryScope)

    require_attempt_ref(reference)

    # Lane/generation labels cannot partition ownership of one remote Attempt.
    return (scope.api_origin, scope.provider_ref, "attempt", reference)


def _start_identity(pending):
    require_type(pending, StartPending)

    # nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
    # nmr_api/operations/execution_attempt_start.py:396-412 reserves keys
    # provider-wide: another Job or lane must not form a separate local owner.
    provider_attempt_key = retained_start_key(pending.body)

    return (pending.scope.api_origin, pending.scope.provider_ref, "start", provider_attempt_key)


class AttemptClaims:
    def __init__(self, *, limits):
        require_type(limits, LiveAttemptLimits)

        self._claims = {}
        self._loop = None
        self._limits = limits

    @classmethod
    # silent-defaults: allow-contract -- Note 010 chooses the shared eight-entry live capacity; consumers explicitly override their own limits.
    def live(cls, *, limits=LiveAttemptLimits()):
        return cls(limits=limits)

    # silent-defaults: allow-contract -- Note 010 reserves the existing 2 MiB wire ceiling unless the live owner declares a smaller sufficient allowance.
    def claim_live(self, pending, *, lifecycle_owner, maximum_terminal_bytes=MAX_COMPLETE_REQUEST_BYTES):
        require_type(pending, StartPending)

        if lifecycle_owner is None:
            raise ValueError(f"{lifecycle_owner=} should not be None")

        # Reject oversized control input before publishing indexes or allowing START.
        # The owner retains the original object; do not keep another encoded copy.
        _encode_pending(pending)

        identities = (
            _start_identity(pending),
            (pending.scope.api_origin, pending.scope.provider_ref, "job", pending.input.job_ref),
        )

        claim = self._reserve(identities, live_terminal_bytes=maximum_terminal_bytes, input_bytes=pending.input.input_byte_length)
        claim._pending = pending
        claim._lifecycle_owner = lifecycle_owner
        claim._maximum_terminal_bytes = claim._pending_terminal_bytes = maximum_terminal_bytes

        return claim

    def require_capacity(self, *, terminal_bytes, input_bytes):
        """Check prospective capacity without reserving or authorizing a Job.

        claim_live still commits exclusion and capacity together. Callers that
        preflight session construction must claim before yielding the event loop.
        """
        require_type(terminal_bytes, int)
        require_type(input_bytes, int)

        if not (16384 <= terminal_bytes <= MAX_COMPLETE_REQUEST_BYTES):
            raise ValueError(
                f"Cannot check Attempt capacity: {terminal_bytes=} must fit the terminal allowance of 16384 to {MAX_COMPLETE_REQUEST_BYTES} bytes"
            )

        if input_bytes < 1:
            raise ValueError(f"Cannot check Attempt capacity: {input_bytes=} must describe a nonempty Job input")

        self._require_live_view()
        existing_claims = set(self._claims.values())
        limits = self._limits
        prospective_attempt_count = len(existing_claims) + 1

        if not (prospective_attempt_count <= limits.maximum_attempts):
            raise LiveCapacityFull(f"{prospective_attempt_count=} should not exceed {limits.maximum_attempts=}")

        # Note 010 reserves the retained START and control metadata
        control_bytes_per_attempt = 131072

        prospective_control_bytes = prospective_attempt_count * control_bytes_per_attempt

        if not (prospective_control_bytes <= limits.maximum_control_bytes):
            raise LiveCapacityFull(f"{prospective_control_bytes=} should not exceed {limits.maximum_control_bytes=}")

        prospective_pending_terminal_bytes = sum(claim._pending_terminal_bytes for claim in existing_claims) + terminal_bytes

        if not (prospective_pending_terminal_bytes <= limits.maximum_pending_terminal_bytes):
            raise LiveCapacityFull(f"{prospective_pending_terminal_bytes=} should not exceed {limits.maximum_pending_terminal_bytes=}")

        prospective_input_bytes = sum(claim._pending.input.input_byte_length for claim in existing_claims) + input_bytes

        if prospective_input_bytes > limits.maximum_input_bytes:
            raise LiveCapacityFull(
                f"Cannot admit another Job: {prospective_input_bytes=} exceeds {limits.maximum_input_bytes=}; existing inputs remain owned"
            )

    def live_owners(self):
        """Keep held controllers reachable when composition selects unrelated work."""
        self._require_live_view()

        # repo-check: allow-tuple-generator -- Snapshot the bounded actual owner inventory without copying lifecycle state.
        return tuple(claim._lifecycle_owner for claim in set(self._claims.values()))

    def owns_job(self, scope, job_ref):
        """Exclude a held Job across lanes without copying its lifecycle state."""
        self._require_live_view()

        require_type(scope, RecoveryScope)

        require_job_ref(job_ref)

        return (scope.api_origin, scope.provider_ref, "job", job_ref) in self._claims

    def _require_live_view(self):
        if (self._loop is not None) and ((caller_loop := asyncio.get_running_loop()) is not self._loop):
            raise OwnershipRefused(f"{id(caller_loop)=} should match {id(self._loop)=}")

    def _reserve(self, identities, *, live_terminal_bytes, input_bytes):
        caller_loop = asyncio.get_running_loop()

        if (self._loop is not None) and (self._loop is not caller_loop):
            raise OwnershipRefused(f"{id(caller_loop)=} should match {id(self._loop)=}")

        for identity in identities:
            if identity in self._claims:
                raise OwnershipRefused(f"{_OWNERSHIP_EVIDENCE.repr(identity)=} should be absent from the claim registry")

        self.require_capacity(terminal_bytes=live_terminal_bytes, input_bytes=input_bytes)
        self._loop = caller_loop

        claim = _AttemptClaim(self, identities)

        for identity in claim._identities:
            self._claims[identity] = claim

        return claim

    def _require(self, claim):
        caller_loop = asyncio.get_running_loop()

        if caller_loop is not self._loop:
            raise OwnershipRefused(f"{id(caller_loop)=} should match {id(self._loop)=}")

        if not (claim.active):
            raise OwnershipRefused(f"{claim.active=} should be True")


class _AttemptClaim:
    def __init__(self, registry, identities):
        self._registry = registry
        self._identities = set(identities)
        self._operation_owner = None
        self._pending = None
        self._lifecycle_owner = None
        self._maximum_terminal_bytes = 0
        self._pending_terminal_bytes = 0

    @property
    def lifecycle_owner(self):
        """Return the actual controller rather than a copied lifecycle phase."""
        self._registry._require(self)

        return self._lifecycle_owner

    @property
    def maximum_terminal_bytes(self):
        self._registry._require(self)

        return self._maximum_terminal_bytes

    @property
    def active(self):
        return all((self._registry._claims.get(identity) is self) for identity in self._identities)

    @property
    def operation_owner(self):
        """Observe the existing borrower so composition settles that exact owner."""
        self._registry._require(self)

        return self._operation_owner

    def require(self, scope, execution_attempt_ref):
        self._registry._require(self)
        requested_attempt_identity = _identity(scope, execution_attempt_ref)

        if not (requested_attempt_identity in self._identities):
            raise OwnershipRefused(f"{_OWNERSHIP_EVIDENCE.repr(requested_attempt_identity)=} should be one of {_OWNERSHIP_EVIDENCE.repr(self._identities)=}")

    def require_live(self, scope):
        self._registry._require(self)

        if self._pending is None:
            raise OwnershipRefused(f"{self._pending is not None=} should be True before a live operation")

        if scope != self._pending.scope:
            require_type(scope, RecoveryScope, error_factory=OwnershipRefused)

            raise OwnershipRefused(metadata_mismatch_message(scope, self._pending.scope))

    def require_live_start(self, pending):
        self.require_live(pending.scope)
        self.require_start(pending)

        for identity in self._identities:
            if identity[2] == "attempt":
                bound_attempt_ref = identity[3]

                raise OwnershipRefused(f"START already has {bound_attempt_ref=}")

    def require_start(self, pending):
        self._registry._require(self)

        if pending != self._pending:
            require_type(pending, type(self._pending), error_factory=OwnershipRefused)

            raise OwnershipRefused(metadata_mismatch_message(pending, self._pending))

    def bind_attempt(self, scope, execution_attempt_ref, *, owner):
        """Add the received reference atomically without releasing the start key."""
        self._registry._require(self)
        identity = _identity(scope, execution_attempt_ref)

        if not (self._operation_owner is owner):
            raise OwnershipRefused(f"{id(owner)=} should match {id(self._operation_owner)=}")

        if not (owner is not None):
            raise OwnershipRefused(f"{owner=} should not be None")

        for known in self._identities:
            if not (known[:2] == identity[:2]):
                raise OwnershipRefused(f"{_OWNERSHIP_EVIDENCE.repr(identity[:2])=} should match {_OWNERSHIP_EVIDENCE.repr(known[:2])=}")

            if not ((known[2] != "attempt") or (known == identity)):
                raise OwnershipRefused(f"{_OWNERSHIP_EVIDENCE.repr(identity)=} should match {_OWNERSHIP_EVIDENCE.repr(known)=}")

        existing = self._registry._claims.get(identity)

        if (existing is not None) and (existing is not self):
            raise OwnershipRefused(f"{_OWNERSHIP_EVIDENCE.repr(identity)=}: {id(existing)=} should match {id(self)=}")

        self._registry._claims[identity] = self

        self._identities.add(identity)

    def begin(self, owner):
        self._registry._require(self)

        if owner is None:
            raise ValueError(f"{owner=} should not be None")

        if not (self._operation_owner is None):
            raise OwnershipRefused(f"{self._operation_owner is None=} should be True before another operation")

        self._operation_owner = owner

    def finish(self, owner):
        self._registry._require(self)

        if not (self._operation_owner is owner):
            raise OwnershipRefused(f"{id(owner)=} should match {id(self._operation_owner)=}")

        self._operation_owner = None

    def release_terminal(self, *, owner):
        """Free the report allowance after the exact borrower confirms its ACK."""
        self._registry._require(self)

        if not (owner is not None):
            raise OwnershipRefused(f"{owner=} should not be None")

        if not (self._operation_owner is owner):
            raise OwnershipRefused(f"{id(owner)=} should match {id(self._operation_owner)=}")

        owner.require_terminal_release_ready(self)
        self._pending_terminal_bytes = 0

    def release(self, *, owner=None):
        self._registry._require(self)

        if not (self._operation_owner is None):
            raise OwnershipRefused(f"{self._operation_owner is None=} should be True before releasing the claim")

        if not (owner is not None):
            raise OwnershipRefused(f"{owner=} should not be None")

        if not (owner is self._lifecycle_owner):
            raise OwnershipRefused(f"{id(owner)=} should match {id(self._lifecycle_owner)=}")

        owner.require_claim_release_ready(self)

        for identity in self._identities:
            del self._registry._claims[identity]
