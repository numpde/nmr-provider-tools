"""A bounded embedded backend for exercising scientific contribution bindings.

Each explicit allocation has one dedicated thread and serves one analysis at a
time. There is no Jobs client, process pool, GPU allocator or crash recovery.
Running Python cannot be forcibly interrupted: stop and deadline observations
retain the original work until it returns. Always observe host retirement.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import math
import time
from uuid import uuid4

from provider_core.async_runner_session import await_original
from provider_core.contribution import ImplementationBinding, ScientificRejection, ScientificReturn, StateUse
from provider_core.hosting import (
    CapacityUnavailable, CleanupObservation, Failure, Invocation, NotAdmitted,
    Observation, OperationRef, Outcome, RequestConflict, ReservationRef, ResultRef,
    StateArgument, StateRef, positive_seconds,
)


@dataclass(frozen=True, slots=True)
class HostLimits:
    """Explicit lifetime bounds; completed operation identities are not evicted."""

    analysis_records: int
    operation_records: int
    input_bytes: int
    result_bytes: int
    state_values: int
    initialization_seconds: float
    retirement_seconds: float

    def __post_init__(self):
        counts = (self.analysis_records, self.operation_records, self.input_bytes,
                  self.result_bytes, self.state_values)
        if not all(type(value) is int and 0 < value for value in counts):
            raise ValueError("Embedded host capacities must be positive integers.")
        positive_seconds(self.initialization_seconds)
        positive_seconds(self.retirement_seconds)


@dataclass(frozen=True, slots=True)
class Allocation:
    """Deployment explicitly assigns a binding and its permitted profiles."""

    name: str
    binding: ImplementationBinding
    profiles: tuple[str, ...]

    def __post_init__(self):
        if not (type(self.name) is str and 0 < len(self.name) <= 160
                and type(self.binding) is ImplementationBinding and type(self.profiles) is tuple):
            raise ValueError("An allocation requires a name, binding and explicit profile tuple.")
        if self.binding.description.contract_version != 1:
            raise ValueError("The embedded backend supports contribution contract version 1 only.")
        required = {self.binding.description.instance_resource_profile}
        required.update(call.description.resource_profile for call in self.binding.calls)
        if not required.issubset(self.profiles):
            raise ValueError("Allocation grants do not cover the binding's declared resource profiles.")


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
        if not (type(wait_seconds) in (int, float) and math.isfinite(wait_seconds) and 0 <= wait_seconds):
            raise ValueError("Observation wait must be finite nonnegative seconds.")
        if after == self.revision and wait_seconds:
            try:
                await asyncio.wait_for(self.event.wait(), timeout=wait_seconds)
            except TimeoutError:
                # Only this event waiter expired. No calculation was cancelled.
                pass


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
        timer = self.loop.call_later(self.seconds, self._expire)
        try:
            await action()
        except BaseException as cause:
            # The request remains an observable owner even when disposal fails.
            self.failures = (Failure("cleanup", cause),)
        else:
            self.settled = True
        finally:
            timer.cancel()
            self.changed()

    async def observe(self, *, after, wait_seconds):
        await self.wait(after, wait_seconds)
        return CleanupObservation(self.revision, self.settled, self.expired, self.failures)


class _Resident:
    def __init__(self, allocation):
        self.allocation = allocation
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-science")
        self.analysis = None
        self.value = None
        self.constructed = False
        self.ready = False
        self.failure = None
        self.dispose_attempted = False
        self.join_task = None

    def initialize(self, seconds):
        if self.failure is not None:
            raise self.failure
        if self.ready:
            return
        hooks = self.allocation.binding.instance
        started = time.monotonic()
        try:
            if hooks is not None:
                self.value = hooks.construct()
                self.constructed = True
                hooks.initialize(self.value)
            if seconds < time.monotonic() - started:
                raise TimeoutError("Instance initialization exceeded its original allowance.")
        except BaseException as cause:
            self.failure = cause
            raise
        self.ready = True

    def dispose(self):
        if self.dispose_attempted:
            raise RuntimeError("Instance disposal was already attempted; retain its earlier evidence.")
        self.dispose_attempted = True
        hooks = self.allocation.binding.instance
        if self.constructed and hooks is not None:
            hooks.dispose(self.value)
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
        hooks = self.resident.allocation.binding.analysis
        try:
            if hooks is not None:
                self.value = hooks.construct(self.resident.value)
                self.constructed = True
                hooks.initialize(self.value)
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


class EmbeddedHost:
    """Administrative owner of explicit allocations, separate from an analysis.

    Resource profiles are checked against deployment assignments. This backend
    bounds threads, records and encoded bytes; it does not sandbox scientific
    code or enforce operating-system memory/device limits.
    """

    def __init__(self, allocations, *, limits):
        if not (type(allocations) is tuple and allocations
                and all(type(item) is Allocation for item in allocations) and type(limits) is HostLimits):
            raise TypeError("Supply explicit Allocation records and HostLimits.")
        if len({item.name for item in allocations}) != len(allocations):
            raise ValueError("Allocation names must be unique within a host.")
        self.loop = asyncio.get_running_loop()
        self.identity = uuid4().hex
        self.limits = limits
        self.residents = {item.name: _Resident(item) for item in allocations}
        self.analyses = {}
        self.operations = {}
        self.input_used = 0
        self.result_reserved = 0
        self.state_reserved = 0
        self.retirement = None

    def _require_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("The embedded host belongs to its original event loop.")

    def open_analysis(self, analysis_id, *, allocation_names):
        """Reserve all named allocations together, without running any science."""
        self._require_loop()
        if self.retirement is not None:
            raise CapacityUnavailable("The host is retiring and refuses new analyses.")
        if not (type(analysis_id) is str and 0 < len(analysis_id) <= 160
                and type(allocation_names) is tuple and allocation_names
                and len(set(allocation_names)) == len(allocation_names)):
            raise ValueError("An analysis needs a name and distinct explicit allocation names.")
        if analysis_id in self.analyses:
            raise RequestConflict("This analysis identity already has an owner.")
        if self.limits.analysis_records <= len(self.analyses):
            raise CapacityUnavailable("The host's analysis-record allowance is full.")
        if any(name not in self.residents for name in allocation_names):
            raise CapacityUnavailable("A requested allocation does not belong to this host.")
        residents = [self.residents[name] for name in allocation_names]
        if any(item.analysis is not None or item.failure is not None for item in residents):
            raise CapacityUnavailable("A requested allocation is occupied or has unresolved initialization.")
        analysis = _Analysis(self, analysis_id, residents)
        self.analyses[analysis_id] = analysis
        for resident in residents:
            resident.analysis = analysis
        return analysis

    def retire(self):
        """Seal admission and settle the original analyses before instance disposal."""
        self._require_loop()
        if self.retirement is None:
            self.retirement = _Cleanup(self.limits.retirement_seconds)
            cleanups = [item.close() for item in self.analyses.values()]
            self.retirement.start(lambda: self._retire(cleanups))
        return self.retirement

    async def _retire(self, cleanups):
        for cleanup in cleanups:
            await asyncio.shield(cleanup.task)
        failures = [failure.cause for item in cleanups for failure in item.failures]
        for resident in self.residents.values():
            if resident.analysis is None:
                try:
                    original = resident.executor.submit(resident.dispose)
                    await await_original(original, deadline=None)
                except BaseException as cause:
                    failures += [cause]
                else:
                    resident.join_task = self.loop.create_task(
                        asyncio.to_thread(resident.executor.shutdown, wait=True)
                    )
                    await asyncio.shield(resident.join_task)
        if failures:
            raise BaseExceptionGroup("Some original resources remain unretired.", failures)


class _Analysis:
    def __init__(self, host, identity, residents):
        self.host = host
        self.identity = identity
        self.science = {item.allocation.name: _ScientificAnalysis(item) for item in residents}
        # repo-check: allow-tuple-generator -- The analysis exposes an immutable reservation inventory.
        self.reservations = tuple(
            ReservationRef(host.identity, identity, item.allocation.name,
                           item.allocation.binding.description.binding_id,
                           item.allocation.binding.description.binding_revision)
            for item in residents
        )
        self.operations = {}
        self.states = {}
        self.closed = False
        self.cleanup = None

    def submit(self, invocation, *, dispatch_guard):
        self.host._require_loop()
        if type(invocation) is not Invocation or not callable(dispatch_guard):
            raise TypeError("Submit requires an Invocation and a host-side permission check.")
        known = self.host.operations.get(invocation.operation_id)
        if known is not None:
            if known.analysis is not self or known.invocation != invocation:
                raise RequestConflict("This operation ID already identifies different work.")
            return known
        refusal = self._refusal(invocation)
        if refusal is not None:
            return NotAdmitted(refusal)
        science = self.science[invocation.reservation.allocation]
        binding = science.resident.allocation.binding
        call = next(item for item in binding.calls if item.description.name == invocation.target.name)
        state_owners = self._state_owners(invocation, call)
        if isinstance(state_owners, NotAdmitted):
            return state_owners
        host = self.host
        outputs = len(call.description.state_outputs)
        if not (len(host.operations) < host.limits.operation_records
                and host.input_used + len(invocation.payload) <= host.limits.input_bytes
                and host.result_reserved + invocation.limits.result_bytes <= host.limits.result_bytes
                and host.state_reserved + outputs <= host.limits.state_values):
            return NotAdmitted("Operation, input, result or scientific-state capacity is full.")
        operation = _Operation(self, science, invocation, call, dispatch_guard, state_owners)
        # Publish owners and capacity before a thread can acquire any resources.
        self.operations[invocation.operation_id] = operation
        host.operations[invocation.operation_id] = operation
        host.input_used += len(invocation.payload)
        host.result_reserved += invocation.limits.result_bytes
        host.state_reserved += outputs
        for owner in state_owners.values():
            owner.claim = operation.reference
        operation.start()
        return operation

    def _refusal(self, invocation):
        if self.closed:
            return "This analysis is closed to new calls."
        if invocation.reservation not in self.reservations:
            return "Reservation belongs to another analysis, host or allocation."
        reservation = invocation.reservation
        if (invocation.target.binding_id, invocation.target.binding_revision) != (
            reservation.binding_id, reservation.binding_revision,
        ):
            return "The selected implementation differs from the reserved binding revision."
        science = self.science[reservation.allocation]
        if science.failure is not None or science.resident.failure is not None:
            return "The original scientific initialization failed and remains owned."
        if invocation.target.name not in {item.description.name for item in science.resident.allocation.binding.calls}:
            return "The selected scientific call is not registered."
        if invocation.limits.input_bytes < len(invocation.payload):
            return "Scientific input exceeds this invocation's byte allowance."
        return None

    def _state_owners(self, invocation, call):
        arguments = {item.name: item.reference for item in invocation.states}
        declared = {item.name: item for item in call.description.state_inputs}
        if len(arguments) != len(invocation.states) or set(arguments) != set(declared):
            return NotAdmitted("State arguments must match the call's distinct declared names.")
        if len(set(arguments.values())) != len(arguments):
            return NotAdmitted("One live state cannot fill several arguments of this embedded call.")
        owners = {}
        for name, reference in arguments.items():
            owner = self.states.get(reference)
            if (owner is None or reference.allocation != invocation.reservation.allocation
                    or reference.binding_revision != invocation.target.binding_revision
                    or reference.kind != declared[name].kind or not owner.available or owner.claim is not None):
                return NotAdmitted("Scientific state is foreign, incompatible, consumed or already in use.")
            owners[name] = owner
        return owners

    def close(self):
        self.host._require_loop()
        if self.cleanup is None:
            self.closed = True
            seconds = min((item.invocation.limits.cleanup_seconds for item in self.operations.values()),
                          default=self.host.limits.retirement_seconds)
            self.cleanup = _Cleanup(seconds)
            for operation in self.operations.values():
                operation.request_stop()
            self.cleanup.start(self._close)
        return self.cleanup

    async def _close(self):
        for operation in self.operations.values():
            await await_original(operation.original, deadline=None)
            await operation.completion.wait()
            if operation.release is not None:
                await asyncio.shield(operation.release.task)
        failures = []
        for allocation, science in self.science.items():
            try:
                original = science.resident.executor.submit(self._dispose_science, allocation, science)
                await await_original(original, deadline=None)
            except BaseException as cause:
                failures += [cause]
            else:
                science.resident.analysis = None
                for operation in self.operations.values():
                    if operation.invocation.reservation.allocation == allocation:
                        self.host.state_reserved -= operation.state_allowance
                        operation.state_allowance = 0
                        if operation.result is None:
                            operation._release_capacity()
        if failures:
            raise BaseExceptionGroup("Some scientific analyses still need cleanup.", failures)

    def _dispose_science(self, allocation, science):
        failures = []
        for operation in self.operations.values():
            if operation.invocation.reservation.allocation == allocation:
                try:
                    operation.dispose_value()
                except BaseException as cause:
                    failures += [cause]
        for reference, owner in self.states.items():
            if reference.allocation == allocation:
                try:
                    owner.dispose()
                except BaseException as cause:
                    failures += [cause]
        if failures:
            raise BaseExceptionGroup("Original scientific values still need cleanup.", failures)
        hooks = science.resident.allocation.binding.analysis
        if science.constructed and hooks is not None:
            hooks.dispose(science.value)
        science.value = None
        science.ready = False


class _Operation(_Changes):
    def __init__(self, analysis, science, invocation, call, guard, states):
        super().__init__()
        self.analysis = analysis
        self.science = science
        self.invocation = invocation
        self.call = call
        self.guard = guard
        self.state_owners = states
        self.reference = OperationRef(analysis.host.identity, analysis.identity, invocation.operation_id)
        self.original = None
        self.completion = asyncio.Event()
        self.dispatched = False
        self.finished = False
        self.outcome = None
        self.failures = ()
        self.result = None
        self.result_released = False
        self.payload = None
        self.returned_states = ()
        self.raw = None
        self.value_disposed = False
        self.disposal_failure = None
        self.stop_requested = False
        self.expired = False
        self.release = None
        self.result_allowance = invocation.limits.result_bytes
        self.state_allowance = len(call.description.state_outputs)
        self.execution_timer = None

    def start(self):
        self.queue_timer = self.loop.call_later(self.invocation.limits.queue_seconds, self._expire)
        try:
            self.original = self.science.resident.executor.submit(self._run)
        except BaseException as cause:
            self.original = Future()
            self.original.set_result(None)
            self.failures = (Failure("dispatch", cause),)
        self.original.add_done_callback(lambda original: self.loop.call_soon_threadsafe(self._finished, original))

    def _expire(self):
        self.expired = True
        self.request_stop()

    def request_stop(self):
        self.require_loop()
        if not self.stop_requested:
            self.stop_requested = True
            self.changed()

    def _guard_at_handoff(self):
        permit = Future()
        self.loop.call_soon_threadsafe(self._admit_dispatch, permit)
        permit.result()

    def _admit_dispatch(self, permit):
        try:
            if self.analysis.closed or self.stop_requested:
                raise PermissionError("This operation was stopped before its next scientific entry.")
            self.guard()
            if not self.dispatched:
                self.dispatched = True
                self.queue_timer.cancel()
                self.execution_timer = self.loop.call_later(self.invocation.limits.execution_seconds, self._expire)
                for parameter in self.call.description.state_inputs:
                    if parameter.use is StateUse.CONSUME:
                        self.state_owners[parameter.name].available = False
            self.changed()
        except BaseException as cause:
            permit.set_exception(cause)
        else:
            # Guard evaluation stays on the authority's loop, after the worker
            # reaches dispatch. No await separates that check from this grant.
            permit.set_result(None)

    def _run(self):
        phase = "permission"
        try:
            self._guard_at_handoff()
            phase = "instance_initialization"
            self._initialize_instance()
            phase = "analysis_initialization"
            self.science.initialize()
            phase = "permission"
            self._guard_at_handoff()
            phase = "decoding"
            decoded = self.call.decode_input(self.science.resident.value, self.science.value, self.invocation.payload)
            phase = "calculation"
            raw = self.call.invoke(self.science.resident.value, self.science.value, decoded,
                                   {name: owner.returned.value for name, owner in self.state_owners.items()})
            self.raw = raw
            self.outcome = Outcome.SUCCEEDED
            phase = "state_adoption"
            if type(raw) is not ScientificReturn:
                raise TypeError("A scientific call binding must return ScientificReturn.")
            adopted = Future()
            self.loop.call_soon_threadsafe(self._adopt_states, raw, adopted)
            adopted.result()
            phase = "encoding"
            encoded = self.call.encode_result(raw.value)
            if not (type(encoded) is bytes and len(encoded) <= self.invocation.limits.result_bytes):
                raise ValueError("Encoded scientific result exceeds its declared byte allowance or is not bytes.")
            self.payload = encoded
            self.result = ResultRef(self.reference, uuid4().hex)
        except ScientificRejection as cause:
            if self.outcome is None and phase in ("decoding", "calculation"):
                self.outcome = Outcome.REJECTED
            self.failures = (Failure(phase, cause),)
        except BaseException as cause:
            if self.outcome is None and phase == "calculation":
                self.outcome = Outcome.FAILED
            self.failures = (Failure(phase, cause),)

    def _initialize_instance(self):
        if self.science.resident.ready:
            return
        timer_ready = Future()
        self.loop.call_soon_threadsafe(self._start_initialization_clock, timer_ready)
        timer = timer_ready.result()
        try:
            self.science.resident.initialize(self.analysis.host.limits.initialization_seconds)
        finally:
            self.loop.call_soon_threadsafe(timer.cancel)

    def _start_initialization_clock(self, timer_ready):
        timer_ready.set_result(self.loop.call_later(
            self.analysis.host.limits.initialization_seconds, self._initialization_expired,
        ))

    def _initialization_expired(self):
        if not self.science.resident.ready:
            self.science.resident.failure = TimeoutError("The original instance initialization has not completed.")
            self._expire()

    def _finished(self, original):
        self.queue_timer.cancel()
        if self.execution_timer is not None:
            self.execution_timer.cancel()
        if original.exception() is not None:
            self.failures += (Failure("backend", original.exception()),)
        for parameter in self.call.description.state_inputs:
            owner = self.state_owners[parameter.name]
            owner.claim = None
            if self.dispatched and self.failures:
                owner.available = False
        if self.raw is None:
            self.analysis.host.state_reserved -= self.state_allowance
            self.state_allowance = 0
            self._release_capacity()
        self.finished = True
        self.completion.set()
        self.changed()

    def _adopt_states(self, raw, adopted):
        # State is owned before encoding, including when a declared output is
        # wrong. Such a contract failure blocks more calls until cleanup.
        returned = []
        failures = []
        consumed = {item.name for item in self.call.description.state_inputs if item.use is StateUse.CONSUME}
        for value in raw.states:
            existing = next((owner for owner in self.analysis.states.values()
                             if not owner.disposed and owner.returned.value is value.value), None)
            if existing is not None:
                if not any(self.state_owners[name] is existing for name in consumed):
                    failures += [ValueError("Returning an existing state requires consuming its original reference.")]
                    continue
                existing.available = False
                existing.disposed = True  # Disposal authority transfers, not physical memory.
            reference = StateRef(self.analysis.host.identity, self.analysis.identity,
                                 self.invocation.reservation.allocation, self.invocation.target.binding_revision,
                                 uuid4().hex, value.kind)
            self.analysis.states[reference] = _State(value, self)
            returned += [StateArgument(value.name, reference)]
        self.returned_states = tuple(returned)
        extra = max(0, len(returned) - self.state_allowance)
        self.analysis.host.state_reserved += extra
        self.state_allowance += extra
        expected = {item.name: item.kind for item in self.call.description.state_outputs}
        actual = {item.name: item.kind for item in raw.states}
        if actual != expected:
            failures += [ValueError("Returned state differs from the call's declared outputs.")]
        if failures:
            cause = ExceptionGroup("Scientific state return violates its binding contract.", failures)
            self.science.failure = cause
            self.science.resident.failure = cause
            adopted.set_exception(cause)
        else:
            adopted.set_result(None)

    async def observe(self, *, after, wait_seconds):
        await self.wait(after, wait_seconds)
        result = self.result if self.finished else None
        return Observation(self.revision, self.dispatched, self.finished, self.outcome, result,
                           self.result_released, self.returned_states, self.failures,
                           self.stop_requested, self.expired)

    def _require_result(self, result):
        self.require_loop()
        if not (type(result) is ResultRef and result == self.result and self.finished):
            raise ValueError("Result reference does not name this operation's established result.")

    async def read_result(self, result):
        self._require_result(result)
        if self.release is not None:
            raise ValueError("Release of this exact result has already been authorized.")
        return self.payload

    def release_result(self, result):
        self._require_result(result)
        if self.release is None:
            self.release = _Cleanup(self.invocation.limits.cleanup_seconds)
            self.release.start(self._release)
        return self.release

    async def _release(self):
        if not self.value_disposed:
            original = self.science.resident.executor.submit(self.dispose_value)
            await await_original(original, deadline=None)
        self.payload = None
        self.result_released = True
        self._release_capacity()
        self.changed()

    def _release_capacity(self):
        self.analysis.host.result_reserved -= self.result_allowance
        self.result_allowance = 0

    def dispose_value(self):
        if self.disposal_failure is not None:
            raise self.disposal_failure
        if self.value_disposed:
            return
        try:
            if self.raw is not None and type(self.raw) is not ScientificReturn:
                raise TypeError("Malformed scientific return has no disposal contract; its allocation remains owned.")
            if type(self.raw) is ScientificReturn and self.raw.dispose_value is not None:
                self.raw.dispose_value(self.raw.value)
        except BaseException as cause:
            self.disposal_failure = cause
            raise
        self.value_disposed = True
        self.raw = None
