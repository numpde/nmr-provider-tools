"""A bounded local backend for scientific contribution bindings.

The provider contributes science and its adapter. This shared backend owns fixed
allocations, operation identity, permission, result custody and retirement.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from uuid import uuid4

from provider_core.async_runner_session import await_original
from provider_core.hosting import (
    AdmissionCode,
    AllocationRef,
    CapacityUnavailable,
    Invocation,
    NotAdmitted,
    RequestConflict,
    ReservationRef,
    ResourceState,
)
from provider_core._embedded_operation import _Operation
from provider_core._embedded_support import (
    EMBEDDED_CAPABILITIES,
    Allocation,
    HostLimits,
    _Cleanup,
    _Resident,
    _ScientificAnalysis,
)


class EmbeddedHost:
    """Administrative owner of fixed local allocations, separate from analyses."""

    capabilities = EMBEDDED_CAPABILITIES

    def __init__(self, allocations, *, limits):
        if not (
            type(allocations) is tuple
            and allocations
            and all(type(item) is Allocation for item in allocations)
            and type(limits) is HostLimits
        ):
            raise TypeError("Supply explicit Allocation records and HostLimits.")
        if len({item.name for item in allocations}) != len(allocations):
            raise ValueError("Allocation names must be unique within a host.")
        self.loop = asyncio.get_running_loop()
        self.identity = uuid4().hex
        self.limits = limits
        self.residents = {
            item.name: _Resident(self.identity, item)
            for item in allocations
        }
        self.analyses = {}
        self.operations = {}
        self.input_used = 0
        self.result_reserved = 0
        self.state_reserved = 0
        self.retirement = None
        self.retiring = False

    @property
    def allocations(self):
        # repo-check: allow-tuple-generator -- The host API returns an allocation snapshot rather than a live view of mutable residents.
        return tuple(resident.reference for resident in self.residents.values())

    def _require_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("The embedded host belongs to its original event loop.")

    def open_analysis(self, analysis_id, *, allocation_names):
        """Reserve all named allocations together, without running any science."""
        self._require_loop()
        if self.retiring:
            raise CapacityUnavailable("The host is retiring and refuses new analyses.")
        if not (
            type(analysis_id) is str
            and 0 < len(analysis_id) <= 160
            and type(allocation_names) is tuple
            and allocation_names
            and len(set(allocation_names)) == len(allocation_names)
        ):
            raise ValueError("An analysis needs a name and distinct explicit allocation names.")
        if analysis_id in self.analyses:
            raise RequestConflict("This analysis identity already has an owner.")
        if self.limits.analysis_records <= len(self.analyses):
            raise CapacityUnavailable("The host's analysis-record allowance is full.")
        if any(name not in self.residents for name in allocation_names):
            raise CapacityUnavailable("A requested allocation does not belong to this host.")

        selected_residents = [self.residents[name] for name in allocation_names]
        if any(
            resident.analysis is not None
            or resident.failure is not None
            or resident.retirement is not None
            or resident.retired
            for resident in selected_residents
        ):
            raise CapacityUnavailable("A requested allocation is occupied, failed or retiring.")

        analysis = _Analysis(self, analysis_id, selected_residents)
        self.analyses[analysis_id] = analysis
        for resident in selected_residents:
            resident.analysis = analysis
        return analysis


    def retire_allocation(self, reference):
        """Pool administration retires one exact allocation and its current analysis."""
        self._require_loop()
        if type(reference) is not AllocationRef or reference.host != self.identity:
            raise ValueError("Allocation retirement requires this host's exact AllocationRef.")
        resident = self.residents.get(reference.allocation)
        if resident is None or resident.reference != reference:
            raise ValueError("Allocation reference does not name this host's current instance.")
        if resident.retirement is None:
            resident.retirement = _Cleanup(self.limits.retirement_seconds)
            resident.retirement.start(lambda: self._retire_resident(resident))
        return resident.retirement

    async def _retire_resident(self, resident):
        if resident.analysis is not None:
            analysis_cleanup = resident.analysis.close()
            await asyncio.shield(analysis_cleanup.task)
            if analysis_cleanup.failures:
                raise BaseExceptionGroup(
                    "The allocation's analysis did not settle.",
                    [failure.cause for failure in analysis_cleanup.failures],
                )
        if resident.analysis is not None:
            raise RuntimeError("The allocation remains attached to an unsettled analysis.")

        disposal_future = resident.executor.submit(resident.dispose)
        await await_original(disposal_future, deadline=None)
        resident.join_task = self.loop.create_task(
            asyncio.to_thread(resident.executor.shutdown, wait=True)
        )
        await asyncio.shield(resident.join_task)
        resident.retired = True

    def retire(self):
        """Seal admission and retire every original allocation."""
        self._require_loop()
        if self.retirement is None:
            self.retiring = True
            self.retirement = _Cleanup(self.limits.retirement_seconds)
            retirements = [
                self.retire_allocation(resident.reference)
                for resident in self.residents.values()
            ]
            self.retirement.start(lambda: self._retire(retirements))
        return self.retirement

    async def _retire(self, retirements):
        failures = []
        for retirement in retirements:
            await asyncio.shield(retirement.task)
            failures.extend(failure.cause for failure in retirement.failures)
        if failures:
            raise BaseExceptionGroup("Some original resources remain unretired.", failures)


class _Analysis:
    def __init__(self, host, identity, residents):
        self.host = host
        self.identity = identity
        self.science = {
            resident.allocation.name: _ScientificAnalysis(resident)
            for resident in residents
        }
        # repo-check: allow-tuple-generator -- Analysis custody retains the exact ordered reservation references admitted at creation.
        self.reservations = tuple(
            ReservationRef(
                host=host.identity,
                analysis=identity,
                allocation=resident.allocation.name,
                binding_id=resident.allocation.binding.description.binding_id,
                binding_revision=resident.allocation.binding.description.binding_revision,
            )
            for resident in residents
        )
        self.operations = {}
        self.states = {}
        self.closed = False
        self.cleanup = None

    def submit(self, invocation, *, dispatch_guard):
        self.host._require_loop()
        if type(invocation) is not Invocation or not callable(dispatch_guard):
            raise TypeError("Submit requires an Invocation and a host-side permission check.")

        operation_key = (self.identity, invocation.operation_id)
        existing_operation = self.host.operations.get(operation_key)
        if existing_operation is not None:
            if (
                existing_operation.analysis is not self
                or existing_operation.invocation != invocation
            ):
                raise RequestConflict("This operation ID already identifies different work.")
            return existing_operation

        admission_refusal = self._refusal(invocation)
        if admission_refusal is not None:
            return admission_refusal

        scientific_analysis = self.science[invocation.reservation.allocation]
        implementation_binding = scientific_analysis.resident.allocation.binding
        selected_call = next(
            item
            for item in implementation_binding.calls
            if item.description.name == invocation.target.name
        )
        state_owners = self._state_owners(invocation, selected_call)
        if isinstance(state_owners, NotAdmitted):
            return state_owners

        host = self.host
        returned_state_count = len(selected_call.description.state_outputs)
        if not (
            len(host.operations) < host.limits.operation_records
            and host.input_used + len(invocation.payload) <= host.limits.input_bytes
            and host.result_reserved + invocation.limits.result_bytes <= host.limits.result_bytes
            and host.state_reserved + returned_state_count <= host.limits.state_values
        ):
            return NotAdmitted(
                code=AdmissionCode.CAPACITY,
                message="Operation, input, result or scientific-state capacity is full.",
            )

        operation = _Operation(
            analysis=self,
            science=scientific_analysis,
            invocation=invocation,
            call=selected_call,
            guard=dispatch_guard,
            states=state_owners,
        )
        self.operations[invocation.operation_id] = operation
        host.operations[operation_key] = operation
        host.input_used += len(invocation.payload)
        host.result_reserved += invocation.limits.result_bytes
        host.state_reserved += returned_state_count
        for owner in state_owners.values():
            owner.claim = operation.reference
        operation.start()
        return operation


    def _refusal(self, invocation):
        if self.closed:
            return NotAdmitted(AdmissionCode.CLOSED, "This analysis is closed to new calls.")
        if invocation.reservation not in self.reservations:
            return NotAdmitted(
                code=AdmissionCode.INVALID_REFERENCE,
                message="Reservation belongs to another analysis, host or allocation.",
            )
        reservation = invocation.reservation
        if (invocation.target.binding_id, invocation.target.binding_revision) != (
            reservation.binding_id,
            reservation.binding_revision,
        ):
            return NotAdmitted(
                code=AdmissionCode.INVALID_REQUEST,
                message="The selected implementation differs from the reserved binding revision.",
            )
        scientific_analysis = self.science[reservation.allocation]
        if (
            scientific_analysis.failure is not None
            or scientific_analysis.resident.failure is not None
        ):
            return NotAdmitted(
                code=AdmissionCode.UNAVAILABLE,
                message="The original scientific initialization failed and remains owned.",
            )
        if invocation.target.name not in {
            item.description.name
            for item in scientific_analysis.resident.allocation.binding.calls
        }:
            return NotAdmitted(
                code=AdmissionCode.INVALID_REQUEST,
                message="The selected scientific call is not registered.",
            )
        if invocation.limits.input_bytes < len(invocation.payload):
            return NotAdmitted(
                code=AdmissionCode.INVALID_REQUEST,
                message="Scientific input exceeds this invocation's byte allowance.",
            )
        return None

    def _state_owners(self, invocation, call):
        state_arguments = {item.name: item.reference for item in invocation.states}
        declared_parameters = {item.name: item for item in call.description.state_inputs}
        if (
            len(state_arguments) != len(invocation.states)
            or set(state_arguments) != set(declared_parameters)
        ):
            return NotAdmitted(
                code=AdmissionCode.INVALID_REQUEST,
                message="State arguments must match the call's distinct declared names.",
            )
        if len(set(state_arguments.values())) != len(state_arguments):
            return NotAdmitted(
                code=AdmissionCode.INVALID_REFERENCE,
                message="One live state cannot fill several arguments of this embedded call.",
            )

        state_owners = {}
        for (name, reference) in state_arguments.items():
            owner = self.states.get(reference)
            if (
                owner is None
                or reference.allocation != invocation.reservation.allocation
                or reference.binding_id != invocation.target.binding_id
                or reference.binding_revision != invocation.target.binding_revision
                or reference.kind != declared_parameters[name].kind
                or not owner.available
                or owner.claim is not None
            ):
                return NotAdmitted(
                    code=AdmissionCode.INVALID_REFERENCE,
                    message="Scientific state is foreign, incompatible, consumed or already in use.",
                )
            state_owners[name] = owner
        return state_owners


    def close(self):
        self.host._require_loop()
        if self.cleanup is None:
            self.closed = True
            cleanup_seconds = min(
                (item.invocation.limits.cleanup_seconds for item in self.operations.values()),
                default=self.host.limits.retirement_seconds,
            )
            self.cleanup = _Cleanup(cleanup_seconds)
            for operation in self.operations.values():
                if not operation.finished:
                    operation.request_stop()
            self.cleanup.start(self._close)
        return self.cleanup

    async def _close(self):
        for operation in self.operations.values():
            await await_original(operation.original, deadline=None)
            await operation.completion.wait()
            if operation.release is not None:
                await asyncio.shield(operation.release.task)

        cleanup_failures = []
        for (allocation_name, scientific_analysis) in self.science.items():
            try:
                disposal_future = scientific_analysis.resident.executor.submit(
                    self._dispose_science,
                    allocation_name,
                    scientific_analysis,
                )
                await await_original(disposal_future, deadline=None)
            except BaseException as cause:
                cleanup_failures.append(cause)
            else:
                scientific_analysis.resident.analysis = None
                for operation in self.operations.values():
                    if operation.invocation.reservation.allocation == allocation_name:
                        self.host.state_reserved -= operation.state_allowance
                        operation.state_allowance = 0
                        operation.resources = ResourceState.RELEASED
                        operation.changed()
                        if operation.result is None:
                            operation._release_capacity()

        if cleanup_failures:
            raise BaseExceptionGroup("Some scientific analyses still need cleanup.", cleanup_failures)

    def _dispose_science(self, allocation_name, scientific_analysis):
        cleanup_failures = []
        for operation in self.operations.values():
            if operation.invocation.reservation.allocation == allocation_name:
                try:
                    operation.dispose_value()
                except BaseException as cause:
                    cleanup_failures.append(cause)

        for (reference, owner) in self.states.items():
            if reference.allocation == allocation_name:
                try:
                    owner.dispose()
                except BaseException as cause:
                    cleanup_failures.append(cause)

        if cleanup_failures:
            raise BaseExceptionGroup(
                "Original scientific values still need cleanup.",
                cleanup_failures,
            )

        hooks = scientific_analysis.resident.allocation.binding.analysis
        if scientific_analysis.constructed and hooks is not None:
            hooks.dispose(scientific_analysis.value)
        scientific_analysis.value = None
        scientific_analysis.ready = False
