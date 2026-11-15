"""Private ownership primitives for the bounded local execution backend."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import math
import time

from provider_core.contribution import (
    ConcurrencyRequirement,
    ImplementationBinding,
    InterruptionSupport,
    IsolationRequirement,
)

from provider_core.hosting import (
    AllocationRef,
    BackendCapabilities,
    CleanupObservation,
    Failure,
    StartDeadlineExpired,
)

from provider_core.scientific import AssetAccess, NoAssets, HostedContribution

# This describes the backend's implemented features, not operator-selectable settings.
# repo-check: allow-module-constant
EMBEDDED_CAPABILITIES = BackendCapabilities(
    contract_versions=frozenset({1}),
    isolation=frozenset({IsolationRequirement.SHARED_PROCESS}),
    concurrency=frozenset({ConcurrencyRequirement.INSTANCE_SERIAL, ConcurrencyRequirement.PARALLEL}),
    interruption=frozenset({InterruptionSupport.NONE, InterruptionSupport.COOPERATIVE}),
    progress=True,
)


def _require_supported_binding(binding):
    """Validate execution features independently of hardware availability."""

    if binding.description.contract_version not in EMBEDDED_CAPABILITIES.contract_versions:
        raise ValueError("The embedded backend does not support this contribution contract version.")

    if binding.description.isolation not in EMBEDDED_CAPABILITIES.isolation:
        raise ValueError("The embedded backend cannot provide this implementation's isolation boundary.")

    for call in binding.description.calls:
        if call.concurrency not in EMBEDDED_CAPABILITIES.concurrency:
            raise ValueError("The embedded backend cannot enforce this call's concurrency boundary.")

        if call.interruption not in EMBEDDED_CAPABILITIES.interruption:
            raise ValueError("The embedded backend cannot provide this call's interruption contract.")

        if call.progress and not EMBEDDED_CAPABILITIES.progress:
            raise ValueError("The embedded backend cannot retain this call's progress events.")


@dataclass(frozen=True, slots=True)
class Allocation:
    """A legacy binding and its assets assigned to one named execution owner."""

    name: str
    binding: ImplementationBinding | HostedContribution
    assets: AssetAccess = field(default_factory=NoAssets)

    def __post_init__(self):
        if type(self.name) is not str:
            raise TypeError("Allocation name must be a string")

        if not isinstance(self.binding, (ImplementationBinding, HostedContribution)):
            raise TypeError(f"Unexpected {type(self.binding)=}; an allocation requires a scientific binding")

        if not 0 < len(self.name) <= 160:
            raise ValueError("Allocation name must contain between 1 and 160 characters.")

        _require_supported_binding(self.binding)


def _require_start_deadline(*, start_not_after: float | None, now: float) -> None:
    """Refuse new scientific entry after the addressed backend's original deadline."""

    if start_not_after is not None and not now < start_not_after:
        raise StartDeadlineExpired(f"Cannot start scientific work: {start_not_after=} has passed at {now=}")


class _Changes:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.revision = 0
        self.event = asyncio.Event()

    def require_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Observe and control this owner on its original event loop.")

    def changed(self):
        self.revision += 1
        self.event.set()
        self.event = asyncio.Event()

    async def wait(self, after, wait_seconds):
        self.require_loop()

        if not (after is None or type(after) is int and 0 <= after <= self.revision):
            raise ValueError("Observation cursor must belong to this owner's current history.")

        if not (
            type(wait_seconds) in (int, float)
            and math.isfinite(wait_seconds)
            and 0 <= wait_seconds
        ):
            raise ValueError("Observation wait must be finite nonnegative seconds.")

        if after == self.revision and wait_seconds:
            try:
                await asyncio.wait_for(self.event.wait(), timeout=wait_seconds)
            except TimeoutError:
                # Only this event waiter expired. No calculation was cancelled.
                return


class _Cleanup(_Changes):
    """Run one cleanup request and report its completion, failures and overdue status."""

    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds
        self.settled = False
        self.expired = False
        self.failures = ()
        self.task = None

    def start(self, action):
        self.task = self.loop.create_task(self._run(action))

    def _expire(self):
        # Observers can see allowance_expired; the cleanup task keeps running.
        self.expired = True
        self.changed()

    async def _run(self, action):
        expiration_timer = self.loop.call_later(self.seconds, self._expire)

        try:
            await action()
        except BaseException as cause:
            self.failures = (Failure("cleanup", cause),)
        else:
            self.settled = True
        finally:
            expiration_timer.cancel()
            self.changed()

    async def observe(self, *, after, wait_seconds):
        await self.wait(after, wait_seconds)

        return CleanupObservation(
            revision=self.revision,
            settled=self.settled,
            allowance_expired=self.expired,
            failures=self.failures,
        )


class _Resident:
    def __init__(self, *, host_identity, name, binding, assets):
        if isinstance(binding, HostedContribution):
            self.reference = binding.allocation
        else:
            self.reference = AllocationRef(
                host=host_identity,
                allocation=name,
                binding_id=binding.description.binding_id,
                binding_revision=binding.description.binding_revision,
            )

        self.name = name
        self.binding = binding
        self.assets = assets

        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="provider-science",
        )

        self.analysis = None
        self.value = None
        self.constructed = False
        self.ready = False
        self.failure = None
        self.dispose_attempted = False
        self.join_task = None
        self.retirement = None
        self.retired = False

    def initialize(self, seconds):
        if self.failure is not None:
            raise self.failure

        if self.ready:
            return

        binding = self.binding
        started_at = time.monotonic()

        try:
            if not self.constructed:
                self.value = binding.construct_instance()
                self.constructed = True

            binding.initialize_instance(instance=self.value, assets=self.assets)

            if seconds < time.monotonic() - started_at:
                raise TimeoutError("Instance initialization exceeded its original allowance.")
        except BaseException as cause:
            self.failure = cause
            raise

        self.ready = True

    def dispose(self):
        if self.dispose_attempted:
            raise RuntimeError("Instance disposal was already attempted; retain its earlier evidence.")

        self.dispose_attempted = True

        if self.constructed:
            self.binding.close_instance(instance=self.value)

        self.value = None
        self.ready = False


class _ScientificAnalysis:
    def __init__(self, resident):
        self.resident = resident
        self.value = None
        self.constructed = False
        self.ready = False
        self.failure = None

    def initialize(self):
        if self.failure is not None:
            raise self.failure

        if self.ready:
            return

        binding = self.resident.binding

        try:
            self.value = binding.construct_analysis(instance=self.resident.value)
            self.constructed = True
            binding.initialize_analysis(analysis=self.value)
        except BaseException as cause:
            self.failure = cause
            raise

        self.ready = True


class _State:
    def __init__(self, returned, operation):
        self.returned = returned
        self.operation = operation
        self.available = True
        self.claim = None
        self.disposed = False
        self.disposal_failure = None

    def dispose(self):
        if self.disposal_failure is not None:
            raise self.disposal_failure

        if self.disposed:
            return

        try:
            self.returned.dispose(self.returned.value)
        except BaseException as cause:
            self.disposal_failure = cause
            raise

        self.disposed = True
        self.available = False
