"""Private ownership primitives for the bounded local execution backend."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
from provider_core.tools import require_nonnegative_number
from provider_core.type_requirements import require_instance, require_type

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

    if not (binding.description.contract_version in EMBEDDED_CAPABILITIES.contract_versions):
        raise ValueError(f"{binding.description.contract_version=} should be in {EMBEDDED_CAPABILITIES.contract_versions=}")

    if not (binding.description.isolation in EMBEDDED_CAPABILITIES.isolation):
        raise ValueError(f"{binding.description.isolation=} should be in {EMBEDDED_CAPABILITIES.isolation=}")

    for call in binding.description.calls:
        if not (call.concurrency in EMBEDDED_CAPABILITIES.concurrency):
            raise ValueError(f"{call.name=}: {call.concurrency=} should be in {EMBEDDED_CAPABILITIES.concurrency=}")

        if not (call.interruption in EMBEDDED_CAPABILITIES.interruption):
            raise ValueError(f"{call.name=}: {call.interruption=} should be in {EMBEDDED_CAPABILITIES.interruption=}")

        if (call.progress) and (not EMBEDDED_CAPABILITIES.progress):
            raise ValueError(f"{EMBEDDED_CAPABILITIES.progress=} should be True for {call.name=} to request progress")


@dataclass(frozen=True, slots=True)
class Allocation:
    """A legacy binding and its assets assigned to one named execution owner."""

    name: str
    binding: ImplementationBinding | HostedContribution
    assets: AssetAccess = field(default_factory=NoAssets)

    def __post_init__(self):
        require_type(self.name, str)

        require_instance(self.binding, (ImplementationBinding, HostedContribution))

        (min_name_characters, max_name_characters) = (1, 160)

        if not (min_name_characters <= (allocation_name_characters := len(self.name)) <= max_name_characters):
            raise ValueError(f"{allocation_name_characters=} should be between {min_name_characters=} and {max_name_characters=}")

        _require_supported_binding(self.binding)


def _require_start_deadline(*, start_not_after: float | None, now: float) -> None:
    """Refuse new scientific entry after the addressed backend's original deadline."""

    if (start_not_after is not None) and (not now < start_not_after):
        raise StartDeadlineExpired(f"Cannot start scientific work: {start_not_after=} has passed at {now=}")


class _Changes:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.revision = 0
        self.event = asyncio.Event()

    def require_loop(self):
        caller_loop = asyncio.get_running_loop()

        if not (caller_loop is self.loop):
            raise RuntimeError(f"{id(caller_loop)=} should match {id(self.loop)=}")

    def changed(self):
        self.revision += 1

        self.event.set()
        self.event = asyncio.Event()

    async def wait(self, after, wait_seconds):
        self.require_loop()

        if after is not None:
            require_type(after, int)

            min_revision = 0

            if not (min_revision <= after <= self.revision):
                raise ValueError(f"{after=} should be between {min_revision=} and {self.revision=}")

        require_nonnegative_number(wait_seconds)

        if (after == self.revision) and wait_seconds:
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
        self.task = self.loop.create_task(self._run_bounded_action(action))

    def _expire(self):
        # Observers can see allowance_expired; the cleanup task keeps running.
        self.expired = True

        self.changed()

    async def _run_bounded_action(self, action):
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
        self.instance = None
        self.constructed = False
        self.ready = False
        self.failure = None
        self.initialization = None
        self.initialization_deadline = None
        self.initialization_abandoned = False
        self.initialization_failure_observed = False
        self.initialization_limit_failure = TimeoutError("Instance initialization exceeded its original allowance.")
        self.dispose_attempted = False
        self.join_task = None
        self.retirement = None
        self.retired = False

    def initialize(self, seconds):
        if not (self.failure is None):
            raise self.failure

        if self.ready:
            return

        binding = self.binding
        started_at = time.monotonic()

        try:
            if not self.constructed:
                self.instance = binding.construct_instance()
                self.constructed = True

            binding.initialize_instance(instance=self.instance, assets=self.assets)

            if seconds < (time.monotonic() - started_at):
                raise self.initialization_limit_failure
        except BaseException as cause:
            self.failure = cause

            raise

        self.ready = True

    def dispose(self):
        if self.dispose_attempted:
            raise RuntimeError(f"{self.dispose_attempted=} should be False")

        self.dispose_attempted = True

        if self.constructed:
            self.binding.close_instance(instance=self.instance)

        self.instance = None
        self.ready = False


class _ScientificAnalysis:
    def __init__(self, resident):
        self.resident = resident
        self.analysis_instance = None
        self.constructed = False
        self.ready = False
        self.failure = None

    def initialize(self):
        if not (self.failure is None):
            raise self.failure

        if self.ready:
            return

        binding = self.resident.binding

        try:
            self.analysis_instance = binding.construct_analysis(instance=self.resident.instance)
            self.constructed = True

            binding.initialize_analysis(analysis=self.analysis_instance)
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
        if not (self.disposal_failure is None):
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
