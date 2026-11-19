"""A bounded local backend for scientific contribution bindings.

The provider contributes science and its adapter. This shared backend owns fixed
allocations, operation identity, permission, result custody and retirement.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import Callable, Mapping
import time
from uuid import uuid4
from weakref import WeakSet

from provider_core.async_runner_session import await_original
from provider_core.contribution import CallBinding, ImplementationBinding, IsolationRequirement, NoInput, ResourceRequirement, ref
from provider_core.scientific import AssetAccess, NoAssets, Contribution, HostedContribution

from provider_core.hosting import (
    AdmissionCode,
    AllocationRef,
    AnalysisCleanup,
    CapacityUnavailable,
    OperationPolicy,
    Invocation,
    NotAdmitted,
    Operation,
    RequestConflict,
    ReservationRef,
    ResourceState,
    settle_cleanup,
    _raise_context_failure,
)

from provider_core.state_slots import StateHandle, StateInputSlot, state_references
from provider_core.tools import require_positive_number

from provider_core._embedded_operation import _Operation

from provider_core._embedded_support import (
    EMBEDDED_CAPABILITIES,
    Allocation,
    _require_supported_binding,
    _require_start_deadline,
    _Cleanup,
    _Resident,
    _ScientificAnalysis,
)

from provider_core.type_requirements import require_instance, require_type


class InProcessExecutionHost:
    """Own adopted scientific objects and their local SDK execution resources.

    An async context joins original retirement before propagating cancellation or
    work failures. Caller-owned request tasks and receivers must finish inside it.
    A cleanup failure remains visible; scope exit does not claim disposal succeeded.
    """

    capabilities = EMBEDDED_CAPABILITIES

    # silent-defaults: allow-contract -- An empty host accepts explicitly adopted contributions.
    def __init__(
        self,
        allocations: tuple[Allocation, ...] = (),
        *,
        input_bytes: int,
        result_bytes: int,
        state_values: int,
        progress_records: int,
        initialization_seconds: float,
        retirement_seconds: float,
    ) -> None:
        """Configure in-flight data capacity and loading/cleanup allowances.

        Byte and state budgets cover currently owned work; they do not impose
        a memory limit on caller-held handles. Time allowances are in seconds.
        """

        for (name, value) in (
            ("input_bytes", input_bytes),
            ("result_bytes", result_bytes),
            ("state_values", state_values),
            ("progress_records", progress_records),
        ):
            if not (type(value) is (expected_type := int)):
                raise TypeError(f"{name=}; {type(value)=} ≠ {expected_type=}")

            if not (0 < value):
                raise ValueError(f"Expected a positive capacity for {name=}; received {value=}")

        for value in (initialization_seconds, retirement_seconds):
            require_positive_number(value)

        require_type(allocations, tuple)

        for allocation in allocations:
            require_type(allocation, Allocation)

        if len({item.name for item in allocations}) != len(allocations):
            raise ValueError("Allocation names must be unique within a host.")

        for allocation in allocations:
            binding = allocation.binding

            require_instance(binding, ImplementationBinding)

        self.loop = asyncio.get_running_loop()
        self.identity = uuid4().hex
        self.input_bytes = input_bytes
        self.result_bytes = result_bytes
        self.state_values = state_values
        self.progress_records = progress_records
        self.initialization_seconds = initialization_seconds
        self.retirement_seconds = retirement_seconds

        self.residents = {
            item.name: _Resident(host_identity=self.identity, name=item.name, binding=item.binding, assets=item.assets)
            for item in allocations
        }

        self.analyses = {}
        self.operations = {}
        self.input_used = 0
        self.result_reserved = 0
        self.state_reserved = 0
        self.retirement = None
        self.retiring = False

    async def __aenter__(self) -> InProcessExecutionHost:
        """Keep this host alive around explicitly nested request/resource owners."""

        return self

    async def __aexit__(self, exception_type, exception, traceback) -> None:
        """Join original SDK retirement, then propagate work or cleanup failures.

        Caller-owned request tasks and receivers must finish inside this context.
        Cancellation is deferred while SDK cleanup is pending, not used to force
        scientific threads to stop. Failure does not claim all resources retired.
        """

        try:
            await settle_cleanup(control=self.retire())
        except BaseException as cleanup:
            _raise_context_failure(original=exception, cleanup=cleanup)

    # silent-defaults: allow-contract -- No file assets, no resident requirements and shared-process execution describe the ordinary embedded contribution contract.
    def adopt(
        self,
        *,
        contribution: Contribution,
        requirements: frozenset[ResourceRequirement],
        assets: AssetAccess = NoAssets(),
        resident_resources: frozenset[ResourceRequirement] = frozenset(),
        isolation: IsolationRequirement = IsolationRequirement.SHARED_PROCESS,
    ) -> HostedContribution:
        """Take ownership of this exact object after validating execution support.

        Rejection leaves ownership with the caller. Successful adoption retains
        the receiver before initialization and permanently forbids re-adoption.
        Existing analyses keep their original allocations; shutdown refuses adoption.
        """
        self._require_loop()

        if self.retiring:
            raise CapacityUnavailable("Cannot adopt a contribution while the host is retiring.")

        allocation_name = uuid4().hex

        hosted = HostedContribution(
            contribution=contribution,
            host=self.identity,
            allocation_name=allocation_name,
            requirements=requirements,
            resident_resources=resident_resources,
            isolation=isolation,
        )

        _require_supported_binding(hosted)
        custody = contribution._custody

        with custody.lock:
            if not (custody.owner is None):
                raise ValueError(f"Cannot adopt this contribution again; {custody.owner=} already owns its lifetime")

            resident = _Resident(host_identity=self.identity, name=allocation_name, binding=hosted, assets=assets)
            resident.value = contribution
            resident.constructed = True
            self.residents[allocation_name] = resident
            custody.owner = resident.reference

        return hosted

    @property
    def allocations(self) -> frozenset[AllocationRef]:
        """Snapshot hosted or unsettled allocations; omit successful retirements."""

        return frozenset(resident.reference for resident in self.residents.values())

    def _require_loop(self):
        if not (asyncio.get_running_loop() is self.loop):
            raise RuntimeError("The embedded host belongs to its original event loop.")

    async def initialize(self, *, allocation: AllocationRef) -> None:
        """Load an adopted contribution before accepting requests for it.

        Concurrent or cancelled waiters observe the same load. A timeout leaves
        that load owned by the host; retirement waits for its worker before close.
        Failed or overdue initialization is not retried on the same contribution.
        """
        self._require_loop()

        require_type(allocation, AllocationRef)

        resident = self.residents.get(allocation.allocation)

        if (resident is None) or (resident.reference != allocation):
            raise ValueError("The allocation handle does not belong to this host")

        if (self.retiring) or (resident.retirement is not None) or (resident.retired):
            raise CapacityUnavailable("Cannot initialize an allocation that is retiring")

        if resident.analysis is not None:
            raise CapacityUnavailable("Cannot initialize an allocation while an analysis owns it")

        if resident.failure is not None:
            if (resident.initialization is not None) and (resident.initialization.done()):
                if resident.initialization.exception() is resident.failure:
                    resident.initialization_abandoned = False
                    resident.initialization_failure_observed = True

            raise resident.failure

        if resident.initialization is None:
            resident.initialization_deadline = time.monotonic() + self.initialization_seconds
            resident.initialization = resident.executor.submit(resident.initialize, self.initialization_seconds)

        try:
            await await_original(resident.initialization, deadline=resident.initialization_deadline)
        except asyncio.CancelledError:
            resident.initialization_abandoned = True

            raise
        except TimeoutError as cause:
            if resident.initialization.done() and (resident.initialization.exception() is cause):
                resident.initialization_abandoned = False
                resident.initialization_failure_observed = True
            else:
                resident.failure = cause
                resident.initialization_abandoned = True

            raise
        except BaseException as cause:
            resident.initialization_abandoned = False

            if resident.initialization.done() and (resident.initialization.exception() is cause):
                resident.initialization_failure_observed = True

            raise
        else:
            resident.initialization_abandoned = False

    def open_analysis(
        self,
        analysis_id: str | None = None,
        *,
        allocation_names: tuple[str, ...] | None = None,
        allocations: frozenset[AllocationRef] | None = None,
    ) -> InProcessAnalysisExecutor:
        """Reserve exact allocation handles and create an analysis identity.

        Explicit analysis_id/allocation_names are the legacy protocol form.
        Handle-based callers retain the returned analysis instead of naming it.
        """
        self._require_loop()

        if allocations is not None:
            if (analysis_id is not None) or (allocation_names is not None):
                raise TypeError("Choose allocation handles or the legacy named analysis arguments")

            require_type(allocations, frozenset)

            for reference in allocations:
                require_type(reference, AllocationRef)

            for reference in allocations:
                resident = self.residents.get(reference.allocation)

                if (resident is None) and (reference.host == self.identity):
                    raise CapacityUnavailable("The reference does not identify a current allocation on this host.")

                if (resident is None) or (resident.reference != reference):
                    raise ValueError("The allocation handle does not belong to this host")

            names = [reference.allocation for reference in allocations]
            allocation_names = tuple(names)
            analysis_id = uuid4().hex

        if self.retiring:
            raise CapacityUnavailable("The host is retiring and refuses new analyses.")

        require_type(analysis_id, str)

        require_type(allocation_names, tuple)

        if not (
            (0 < len(analysis_id) <= 160) and
            (allocation_names) and
            (len(set(allocation_names)) == len(allocation_names))
        ):
            raise ValueError("An analysis needs a name and distinct explicit allocation names.")

        if analysis_id in self.analyses:
            raise RequestConflict("This analysis identity already has an owner.")

        if any((name not in self.residents) for name in allocation_names):
            raise CapacityUnavailable("A requested allocation does not belong to this host.")

        selected_residents = [self.residents[name] for name in allocation_names]

        if any(
            (
                (
                    (resident.analysis is not None) or
                    ((resident.initialization is not None) and (not resident.initialization.done())) or
                    (resident.failure is not None) or
                    (resident.retirement is not None) or
                    resident.retired
                )
                for resident in selected_residents
            ),
        ):
            raise CapacityUnavailable("A requested allocation is occupied, failed or retiring.")

        analysis_executor = InProcessAnalysisExecutor(
            execution_host=self,
            analysis_id=analysis_id,
            residents=selected_residents,
        )

        self.analyses[analysis_id] = analysis_executor

        for resident in selected_residents:
            resident.analysis = analysis_executor

        return analysis_executor

    def retire_allocation(self, reference):
        """Retire one allocation and its analysis; retain the returned control.

        Successful retirement removes the allocation from the host. The retained
        control still reports its outcome; failed cleanup stays owned by the host.
        """
        self._require_loop()

        require_type(reference, AllocationRef)

        if reference.host != self.identity:
            raise ValueError("Allocation retirement requires this host's exact AllocationRef.")

        resident = self.residents.get(reference.allocation)

        if (resident is None) or (resident.reference != reference):
            raise ValueError("Allocation reference does not name this host's current instance.")

        if resident.retirement is None:
            resident.retirement = _Cleanup(self.retirement_seconds)

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

        if not (resident.analysis is None):
            raise RuntimeError("The allocation remains attached to an unsettled analysis.")

        disposal_future = resident.executor.submit(resident.dispose)

        failures = []

        try:
            await await_original(disposal_future, deadline=None)
        except BaseException as cause:
            failures.append(cause)

        if (resident.initialization_abandoned) and (not resident.initialization_failure_observed):
            initialization_failure = resident.initialization.exception()

            if (initialization_failure is not None) and (initialization_failure is not resident.initialization_limit_failure):
                failures.append(initialization_failure)

        resident.join_task = self.loop.create_task(
            asyncio.to_thread(resident.executor.shutdown, wait=True)
        )

        await asyncio.shield(resident.join_task)

        if len(failures) == 1:
            raise failures[0]

        if failures:
            raise BaseExceptionGroup("Allocation initialization or cleanup failed.", failures)

        resident.retired = True

        del self.residents[resident.name]

    def retire(self):
        """Request cleanup of all hosted scientific objects and refuse new analyses.

        Provider shutdown code retains and observes the returned control. Exceeding
        retirement_seconds marks cleanup overdue but does not stop its thread.
        """
        self._require_loop()

        if self.retirement is None:
            self.retiring = True
            self.retirement = _Cleanup(self.retirement_seconds)

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


class InProcessAnalysisExecutor:
    """In-process executor returned by InProcessExecutionHost.open_analysis.

    Its reservations belong to that host. Both bound-call and portable
    invocation submission use the same admission and operation owners.
    """

    def __init__(self, *, execution_host, analysis_id, residents):
        self.host = execution_host
        self.identity = analysis_id

        self.science = {
            resident.name: _ScientificAnalysis(resident)
            for resident in residents
        }

        self.reservations = frozenset(
            ReservationRef(
                host=execution_host.identity,
                analysis=analysis_id,
                allocation=resident.name,
                binding_id=resident.binding.description.binding_id,
                binding_revision=resident.binding.description.binding_revision,
            )
            for resident in residents
        )

        self.operations = {}

        # Observe resource-release changes through surviving handles without
        # making their completed operations owned by this analysis.
        self.observed_operations = WeakSet()

        self.states = {}
        self.closed = False
        self.cleanup = None

    def reservation(self, *, allocation: AllocationRef) -> ReservationRef:
        """Return this analysis's reservation for an exact allocation handle."""
        self.host._require_loop()

        require_type(allocation, AllocationRef)

        resident = self.host.residents.get(allocation.allocation)

        if (resident is None) or (resident.reference != allocation):
            raise ValueError("The allocation handle does not belong to this host")

        for reservation in self.reservations:
            if reservation.allocation == allocation.allocation:
                return reservation

        raise ValueError("This analysis did not reserve the supplied allocation")

    # Omitted start deadlines leave the execution policy's clocks in force.
    def submit_call[InputT, StateT, OutputT](
        self,
        call: CallBinding[InputT, StateT, OutputT],
        *,
        payload: bytes,
        states: Mapping[StateInputSlot, StateHandle],
        reservation: ReservationRef,
        policy: OperationPolicy,
        start_not_after: float | None = None,
    ) -> Operation | NotAdmitted:
        """Ask the SDK to execute a registered scientific method with encoded input.

        Provider code supplies an owned CallBinding, its reservation and policy.
        Execution checks its own stopped/closed state and any start deadline before
        admission and again before scientific entry. A start deadline uses this
        backend's event-loop clock and cannot be renewed by retrying the request.
        A returned handle means the request was admitted, not that it ran or succeeded.
        """

        require_type(call, CallBinding)

        invocation = Invocation(
            operation_id=uuid4().hex,
            target=ref(call),
            payload=payload,
            states=state_references(states=states),
            reservation=reservation,
            policy=policy,
            start_not_after=start_not_after,
        )

        return self._submit(
            invocation=invocation,
            retain_identity=False,
        )

    # silent-defaults: allow-contract -- NoInput represents absent payload; omitted start deadlines leave policy clocks in force.
    def submit_method(
        self,
        *,
        method: Callable,
        scientific_input: object = NoInput(),
        states: Mapping[StateInputSlot, StateHandle],
        reservation: ReservationRef,
        policy: OperationPolicy,
        start_not_after: float | None = None,
    ) -> Operation | NotAdmitted:
        """Ask the SDK to run an exported method on the reserved scientific object.

        Provider application code passes a bound method such as object.prepare.
        Encode its input, then use submit_call for admission and execution. The
        returned operation handle tracks this submission through completion;
        submitting the method again requests new work.
        """
        self.host._require_loop()
        refusal = self._reservation_refusal(reservation=reservation)

        if refusal is not None:
            return refusal

        hosted = self.science[reservation.allocation].resident.binding

        require_instance(hosted, HostedContribution)

        if not (getattr(method, "__self__", None) is hosted.contribution):
            raise ValueError("Submit an exported bound method of the reserved contribution")

        call = hosted.call(method=method)

        if (type(scientific_input) is NoInput) and (call.input_codec.value_type is not NoInput):
            raise TypeError(f"Unexpected {type(scientific_input)=}; {call.description.name=} requires {call.input_codec.value_type=}")

        return self.submit_call(
            call=call,
            payload=call.encode_input(value=scientific_input),
            states=states,
            reservation=reservation,
            policy=policy,
            start_not_after=start_not_after,
        )

    def submit(
        self,
        invocation: Invocation,
    ) -> Operation | NotAdmitted:
        """Retain portable request identity until this analysis closes."""

        return self._submit(
            invocation=invocation,
            retain_identity=True,
        )

    def _submit(self, invocation, *, retain_identity):
        """Admit work and retain portable request identity for the open analysis."""
        self.host._require_loop()

        require_type(invocation, Invocation)

        operation_key = (self.identity, invocation.operation_id)

        if self.closed:
            return NotAdmitted(AdmissionCode.CLOSED, "This analysis is closed to new calls.")

        existing_operation = self.operations.get(invocation.operation_id)

        if existing_operation is not None:
            if (
                (existing_operation.analysis is not self) or
                (existing_operation.invocation != invocation)
            ):
                raise RequestConflict("This operation ID already identifies different work.")

            existing_operation.retain_identity |= retain_identity

            return existing_operation

        if operation_key in self.host.operations:
            raise RequestConflict("An earlier analysis still owns this operation identity.")

        admission_refusal = self._refusal(invocation)

        if admission_refusal is not None:
            return admission_refusal

        _require_start_deadline(start_not_after=invocation.start_not_after, now=self.host.loop.time())
        scientific_analysis = self.science[invocation.reservation.allocation]
        implementation_binding = scientific_analysis.resident.binding

        selected_call = next(
            item
            for item in implementation_binding._calls
            if item.description.name == invocation.target.name
        )

        state_owners = self._state_owners(invocation, selected_call)

        if isinstance(state_owners, NotAdmitted):
            return state_owners

        host = self.host
        returned_state_count = len(selected_call.description.state_outputs)

        if not (
            ((host.input_used + len(invocation.payload)) <= host.input_bytes) and
            ((host.result_reserved + invocation.policy.reject_encoded_result_over_bytes) <= host.result_bytes) and
            ((host.state_reserved + returned_state_count) <= host.state_values)
        ):
            return NotAdmitted(
                code=AdmissionCode.CAPACITY,
                message="Input, result or scientific-state capacity is full.",
            )

        # These checks admitted one scientific-method submission from provider code.
        # Reserve its capacity and queue its worker; it has not begun science yet.
        operation = _Operation(
            analysis=self,
            science=scientific_analysis,
            invocation=invocation,
            call=selected_call,
            states=state_owners,
            retain_identity=retain_identity,
        )

        self.observed_operations.add(operation)
        self.operations[invocation.operation_id] = operation
        host.operations[operation_key] = operation
        host.input_used += len(invocation.payload)
        host.result_reserved += invocation.policy.reject_encoded_result_over_bytes
        host.state_reserved += returned_state_count

        for owner in state_owners.values():
            owner.claim = operation.reference

        operation.start()

        return operation


    def _reservation_refusal(self, *, reservation):
        if reservation not in self.reservations:
            return NotAdmitted(
                code=AdmissionCode.INVALID_REFERENCE,
                message="Reservation belongs to another analysis, host or allocation.",
            )

        return None

    def _refusal(self, invocation):
        if self.closed:
            return NotAdmitted(AdmissionCode.CLOSED, "This analysis is closed to new calls.")

        refusal = self._reservation_refusal(reservation=invocation.reservation)

        if refusal is not None:
            return refusal

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
            (scientific_analysis.failure is not None) or
            (scientific_analysis.resident.failure is not None)
        ):
            return NotAdmitted(
                code=AdmissionCode.UNAVAILABLE,
                message="The original scientific initialization failed and remains owned.",
            )

        if (
            invocation.target.name not in {
                item.description.name
                for item in scientific_analysis.resident.binding._calls
            }
        ):
            return NotAdmitted(
                code=AdmissionCode.INVALID_REQUEST,
                message="The selected scientific call is not registered.",
            )

        if invocation.policy.reject_encoded_input_over_bytes < len(invocation.payload):
            return NotAdmitted(
                code=AdmissionCode.INVALID_REQUEST,
                message="Scientific input exceeds this invocation's byte allowance.",
            )

        return None

    def _state_owners(self, invocation, call):
        state_arguments = invocation.states
        declared_parameters = {item.name: item for item in call.description.state_inputs}

        if set(state_arguments) != set(declared_parameters):
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
                (owner is None) or
                (reference.allocation != invocation.reservation.allocation) or
                (reference.binding_id != invocation.target.binding_id) or
                (reference.binding_revision != invocation.target.binding_revision) or
                (reference.kind != declared_parameters[name].kind) or
                (type(owner.returned.value) is not call.state_binding.input_types[name]) or
                (not owner.available) or
                (owner.claim is not None)
            ):
                return NotAdmitted(
                    code=AdmissionCode.INVALID_REFERENCE,
                    message="Scientific state is foreign, incompatible, consumed or already in use.",
                )

            state_owners[name] = owner

        return state_owners


    def request_stop(self) -> None:
        """Refuse further science and request stops without disposing retained results.

        Provider code calls this when a job is canceled. Receivers can still finish
        using retained results. Release results and close the session separately
        when their consumers no longer need its resources.
        """
        self.host._require_loop()
        self.closed = True

        for operation in list(self.operations.values()):
            if not operation.finished:
                operation.request_stop()

    def close(self) -> AnalysisCleanup:
        """Request cleanup of this execution session and return its retained control.

        Provider code closes the session after its scientific work or during
        shutdown. Refuse later submissions, request stops for unfinished methods,
        then wait for them before disposing retained science state. The cleanup
        timer only reports overdue status; it does not cancel that cleanup.
        """
        self.host._require_loop()

        if self.cleanup is None:
            self.request_stop()

            cleanup_overdue_flag_seconds = min(
                (item.invocation.policy.cleanup_overdue_flag_seconds for item in self.operations.values()),
                default=self.host.retirement_seconds,
            )

            self.cleanup = _Cleanup(cleanup_overdue_flag_seconds)

            self.cleanup.start(self._close)

        return self.cleanup

    async def _close(self):
        for operation in list(self.operations.values()):
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

                for operation in list(self.observed_operations):
                    if operation.invocation.reservation.allocation == allocation_name:
                        self.host.state_reserved -= operation.state_allowance
                        operation.state_allowance = 0
                        operation.resources = ResourceState.RELEASED

                        operation.changed()

                        if operation.result is None:
                            operation._release_capacity()

        if cleanup_failures:
            raise BaseExceptionGroup("Some scientific analyses still need cleanup.", cleanup_failures)

        # Portable request deduplication lasts only while this analysis is open.
        # Result handles keep their payloads; the host separately retains any
        # unreleased result or failed cleanup obligation.
        for operation in list(self.operations.values()):
            operation._forget_settled()

        self.operations.clear()
        self.observed_operations.clear()
        self.states.clear()
        del self.host.analyses[self.identity]


    def _dispose_science(self, allocation_name, scientific_analysis):
        cleanup_failures = []

        for operation in list(self.operations.values()):
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

        if scientific_analysis.constructed:
            scientific_analysis.resident.binding.close_analysis(analysis=scientific_analysis.value)

        scientific_analysis.value = None
        scientific_analysis.ready = False
