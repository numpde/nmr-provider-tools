"""Private ownership primitives for the bounded local execution backend."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import math
import time

from provider_core.contribution import (
    ConcurrencyRequirement,
    ImplementationBinding,
    InterruptionSupport,
    IsolationRequirement,
    ResourceRequirement,
)
from provider_core.hosting import (
    AllocationRef,
    BackendCapabilities,
    CleanupObservation,
    Failure,
    ResourceGrant,
    positive_seconds,
)

# This describes the backend's implemented features, not operator-selectable settings.
# repo-check: allow-module-constant
EMBEDDED_CAPABILITIES = BackendCapabilities(
    contract_versions=(1,),
    isolation=(IsolationRequirement.SHARED_PROCESS,),
    concurrency=(
        ConcurrencyRequirement.INSTANCE_SERIAL,
        ConcurrencyRequirement.PARALLEL,
    ),
    interruption=(InterruptionSupport.NONE, InterruptionSupport.COOPERATIVE),
    progress=True,
)


@dataclass(frozen=True, slots=True)
class HostLimits:
    """Explicit lifetime bounds; completed operation identities are not evicted."""

    analysis_records: int
    operation_records: int
    input_bytes: int
    result_bytes: int
    state_values: int
    progress_records: int
    initialization_seconds: float
    retirement_seconds: float

    def __post_init__(self):
        capacity_counts = (
            self.analysis_records,
            self.operation_records,
            self.input_bytes,
            self.result_bytes,
            self.state_values,
            self.progress_records,
        )
        if not all(type(value) is int and 0 < value for value in capacity_counts):
            raise ValueError("Embedded host capacities must be positive integers.")
        positive_seconds(self.initialization_seconds)
        positive_seconds(self.retirement_seconds)


def _combined_requirements(resident_requirements, call_requirements):
    combined_requirements = {}
    for requirement in (*resident_requirements, *call_requirements):
        previous_requirement = combined_requirements.get(requirement.identity)
        if previous_requirement is None:
            combined_requirements[requirement.identity] = requirement
            continue

        combined_requirements[requirement.identity] = ResourceRequirement(
            kind=requirement.kind,
            amount=previous_requirement.amount + requirement.amount,
            unit=requirement.unit,
            selector=requirement.selector,
            exclusive=previous_requirement.exclusive or requirement.exclusive,
        )

    return tuple(combined_requirements.values())


def _grants_cover(grants, requirements):
    return all(any(grant.covers(requirement) for grant in grants) for requirement in requirements)


@dataclass(frozen=True, slots=True)
class Allocation:
    """Deployment explicitly assigns a configured binding and resource grants."""

    name: str
    binding: ImplementationBinding
    grants: tuple[ResourceGrant, ...]

    def __post_init__(self):
        if not (
            type(self.name) is str
            and 0 < len(self.name) <= 160
            and type(self.binding) is ImplementationBinding
            and type(self.grants) is tuple
            and all(type(grant) is ResourceGrant for grant in self.grants)
        ):
            raise ValueError("An allocation requires a name, binding and explicit resource grants.")
        if len({grant.identity for grant in self.grants}) != len(self.grants):
            raise ValueError("Allocation resource grants cannot contain duplicate identities.")
        if self.binding.description.contract_version not in EMBEDDED_CAPABILITIES.contract_versions:
            raise ValueError("The embedded backend does not support this contribution contract version.")
        if self.binding.description.isolation not in EMBEDDED_CAPABILITIES.isolation:
            raise ValueError("The embedded backend cannot provide this implementation's isolation boundary.")
        for call in self.binding.description.calls:
            if call.concurrency not in EMBEDDED_CAPABILITIES.concurrency:
                raise ValueError("The embedded backend cannot enforce this call's concurrency boundary.")
            if call.interruption not in EMBEDDED_CAPABILITIES.interruption:
                raise ValueError("The embedded backend cannot provide this call's interruption contract.")
            if call.progress and not EMBEDDED_CAPABILITIES.progress:
                raise ValueError("The embedded backend cannot retain this call's progress events.")
            required_resources = _combined_requirements(
                self.binding.description.resident_resources,
                call.resources,
            )
            if not _grants_cover(self.grants, required_resources):
                raise ValueError("Allocation grants do not cover one declared resident-plus-call requirement.")


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
    def __init__(self, host_identity, allocation):
        self.reference = AllocationRef(
            host=host_identity,
            allocation=allocation.name,
            binding_id=allocation.binding.description.binding_id,
            binding_revision=allocation.binding.description.binding_revision,
        )
        self.allocation = allocation
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

        instance_hooks = self.allocation.binding.instance
        started_at = time.monotonic()
        try:
            if instance_hooks is not None:
                self.value = instance_hooks.construct()
                self.constructed = True
                instance_hooks.initialize(self.value)
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

        instance_hooks = self.allocation.binding.instance
        if self.constructed and instance_hooks is not None:
            instance_hooks.dispose(self.value)
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

        analysis_hooks = self.resident.allocation.binding.analysis
        try:
            if analysis_hooks is not None:
                self.value = analysis_hooks.construct(self.resident.value)
                self.constructed = True
                analysis_hooks.initialize(self.value)
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
