"""One owned calculation in the bounded local execution backend."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import threading
from concurrent.futures import Future
from uuid import uuid4

from provider_core._embedded_support import _Changes, _Cleanup, _State, _require_start_deadline
from provider_core.async_runner_session import await_original
from provider_core.contribution import (
    ProgressEvent,
    ScientificReturn,
    StateUse,
)
from provider_core.hosting import (
    AcceptanceState,
    DispatchState,
    Failure,
    Observation,
    OperationRef,
    Outcome,
    ResourceState,
    ResultRef,
    ResultState,
    StateResult,
    StateRef,
)
from provider_core.payload import CallRejected


class _OperationControl:
    def __init__(self, operation):
        self.operation = operation

    @property
    def stop_requested(self):
        return self.operation.stop_signal.is_set()

    def report_progress(self, event):
        if not self.operation.call.description.progress:
            raise RuntimeError("This call did not declare progress reporting.")
        if type(event) is not ProgressEvent:
            raise TypeError("Scientific progress must be a ProgressEvent.")
        progress_receipt = Future()
        self.operation.loop.call_soon_threadsafe(
            self.operation._record_progress,
            event,
            progress_receipt,
        )
        progress_receipt.result()


class _Operation(_Changes):
    def __init__(self, analysis, science, invocation, call, states, *, retain_identity):
        super().__init__()
        self.analysis = analysis
        self.retain_identity = retain_identity
        self.science = science
        self.invocation = invocation
        self.call = call
        self.state_owners = states
        self.reference = OperationRef(
            host=analysis.host.identity,
            analysis=analysis.identity,
            operation_id=invocation.operation_id,
        )
        self.original = None
        self.completion = asyncio.Event()
        self.dispatch = DispatchState.QUEUED
        self.finished = False
        self.outcome = None
        self.failures = ()
        self.result = None
        self.result_state = ResultState.NONE
        self.payload = None
        self.returned_states = ()
        self.progress = []
        self.progress_dropped = 0
        self.raw = None
        self.value_disposed = False
        self.disposal_failure = None
        self.stop_requested = False
        self.stop_signal = threading.Event()
        self.expired = False
        self.release = None
        self.result_allowance = invocation.policy.reject_encoded_result_over_bytes
        self.state_allowance = len(call.description.state_outputs)
        self.execution_timer = None
        self.resources = ResourceState.RESERVED
        self.control = _OperationControl(self)

    def start(self):
        """Start the admitted submission's queue clock and enqueue its worker."""
        self.queue_timer = self.loop.call_later(
            self.invocation.policy.queue_stop_request_seconds,
            self._expire,
        )
        try:
            self.original = self.science.resident.executor.submit(self._run)
        except BaseException as cause:
            self.original = Future()
            self.original.set_result(None)
            self.failures = (Failure("dispatch", cause),)
        self.original.add_done_callback(
            lambda execution_future: self.loop.call_soon_threadsafe(
                self._finished,
                execution_future,
            )
        )

    def _expire(self):
        self.expired = True
        self.request_stop()

    def request_stop(self):
        """Set the stop signal checked before science starts and by cooperative code."""
        self.require_loop()
        if not self.stop_requested:
            self.stop_requested = True
            self.stop_signal.set()
            self.changed()

    def _check_execution_at_handoff(self):
        """Ask the owning event loop to check execution state before scientific entry."""
        permit = Future()
        self.loop.call_soon_threadsafe(self._check_dispatch, permit)
        permit.result()

    def _check_dispatch(self, permit):
        """Enforce execution's stopped/closed state and original start deadline."""
        try:
            if self.analysis.closed or self.stop_requested:
                raise PermissionError(
                    "This operation was stopped before its next scientific entry."
                )
            _require_start_deadline(
                start_not_after=self.invocation.start_not_after,
                now=self.loop.time(),
            )
            if self.dispatch is DispatchState.QUEUED:
                self.dispatch = DispatchState.STARTED
                self.resources = ResourceState.OCCUPIED
                # The worker may now initialize the scientific object and run its
                # method. Replace the waiting clock with the execution clock.
                self.queue_timer.cancel()
                self.execution_timer = self.loop.call_later(
                    self.invocation.policy.execution_stop_request_seconds,
                    self._expire,
                )
                # Consume state only after the scientific execution begins.
                for parameter in self.call.description.state_inputs:
                    if parameter.use is StateUse.CONSUME:
                        self.state_owners[parameter.name].available = False
            self.changed()
        except BaseException as cause:
            permit.set_exception(cause)
        else:
            permit.set_result(None)

    def _run(self):
        phase = "start"
        try:
            self._check_execution_at_handoff()

            phase = "instance_initialization"
            self._initialize_instance()

            phase = "analysis_initialization"
            self.science.initialize()

            phase = "start"
            self._check_execution_at_handoff()

            phase = "decoding"
            scientific_input = self.call.decode_input(self.invocation.payload)

            phase = "calculation"
            scientific_return = self.call.invoke(
                self.science.resident.value,
                self.science.value,
                scientific_input,
                {
                    name: owner.returned.value
                    for (name, owner) in self.state_owners.items()
                },
                self.control,
            )
            self.raw = scientific_return
            self.outcome = Outcome.SUCCEEDED

            phase = "state_adoption"
            if type(scientific_return) is not ScientificReturn:
                raise TypeError("A scientific call binding must return ScientificReturn.")

            adoption_receipt = Future()
            self.loop.call_soon_threadsafe(
                self._adopt_states,
                scientific_return,
                adoption_receipt,
            )
            adoption_receipt.result()

            phase = "encoding"
            result_payload = self.call.encode_result(scientific_return.value)
            if type(result_payload) is not bytes:
                raise TypeError(f"Unexpected {type(result_payload)=}; encoded scientific results must be bytes")
            if not len(result_payload) <= self.invocation.policy.reject_encoded_result_over_bytes:
                raise ValueError(
                    "Encoded scientific result exceeds its declared byte allowance."
                )

            self.payload = result_payload
            self.result = ResultRef(self.reference, uuid4().hex)
            self.result_state = ResultState.AVAILABLE
        except CallRejected as cause:
            if self.outcome is None and phase in ("decoding", "calculation"):
                self.outcome = Outcome.REJECTED
            self.failures = (Failure(phase, cause),)
        except BaseException as cause:
            if self.outcome is None and phase == "calculation":
                self.outcome = Outcome.FAILED
            if self.outcome is Outcome.SUCCEEDED and phase == "encoding":
                self.result_state = ResultState.ENCODING_FAILED
            self.failures = (Failure(phase, cause),)

    def _initialize_instance(self):
        if self.science.resident.ready:
            return
        timer_receipt = Future()
        self.loop.call_soon_threadsafe(
            self._start_initialization_clock,
            timer_receipt,
        )
        initialization_timer = timer_receipt.result()
        try:
            self.science.resident.initialize(
                self.analysis.host.initialization_seconds
            )
        finally:
            self.loop.call_soon_threadsafe(initialization_timer.cancel)

    def _start_initialization_clock(self, timer_receipt):
        timer_receipt.set_result(
            self.loop.call_later(
                self.analysis.host.initialization_seconds,
                self._initialization_expired,
            )
        )

    def _initialization_expired(self):
        if not self.science.resident.ready:
            self.science.resident.failure = TimeoutError(
                "The original instance initialization has not completed."
            )
            self._expire()

    def _record_progress(self, event, receipt):
        try:
            if len(self.progress) == self.analysis.host.progress_records:
                del self.progress[0]
                self.progress_dropped += 1
            self.progress.append(event)
            self.changed()
        except BaseException as cause:
            receipt.set_exception(cause)
        else:
            receipt.set_result(None)

    def _finished(self, execution_future):
        self.queue_timer.cancel()
        if self.execution_timer is not None:
            self.execution_timer.cancel()
        if execution_future.exception() is not None:
            self.failures += (Failure("backend", execution_future.exception()),)
        if self.dispatch is DispatchState.QUEUED:
            self.dispatch = DispatchState.NOT_STARTED

        for parameter in self.call.description.state_inputs:
            owner = self.state_owners[parameter.name]
            owner.claim = None
            if self.dispatch is DispatchState.STARTED and self.failures:
                owner.available = False

        if self.raw is None:
            self.value_disposed = True
            self.analysis.host.state_reserved -= self.state_allowance
            self.state_allowance = 0
            self._release_capacity()

        self.analysis.host.input_used -= len(self.invocation.payload)
        self.finished = True
        self.completion.set()
        self._forget_settled()
        self.changed()

    def _adopt_states(self, scientific_return, adoption_receipt):
        returned_arguments = []
        contract_failures = []
        consumed_state_names = {
            item.name
            for item in self.call.description.state_inputs
            if item.use is StateUse.CONSUME
        }
        for returned_state in scientific_return.states:
            existing_owner = next(
                (
                    owner
                    for owner in self.analysis.states.values()
                    if not owner.disposed and owner.returned.value is returned_state.value
                ),
                None,
            )
            if existing_owner is not None:
                if not any(
                    self.state_owners[name] is existing_owner
                    for name in consumed_state_names
                ):
                    contract_failures.append(
                        ValueError(
                            "Returning an existing state requires consuming its "
                            "original reference."
                        )
                    )
                    continue
                existing_owner.available = False
                existing_owner.disposed = True

            state_reference = StateRef(
                host=self.analysis.host.identity,
                analysis=self.analysis.identity,
                allocation=self.invocation.reservation.allocation,
                binding_id=self.invocation.target.binding_id,
                binding_revision=self.invocation.target.binding_revision,
                state_id=uuid4().hex,
                kind=returned_state.kind,
                producer=self.reference,
            )
            self.analysis.states[state_reference] = _State(returned_state, self)
            returned_arguments.append(StateResult(returned_state.name, state_reference))

        self.returned_states = tuple(returned_arguments)
        additional_state_reservations = max(0, len(returned_arguments) - self.state_allowance)
        self.analysis.host.state_reserved += additional_state_reservations
        self.state_allowance += additional_state_reservations

        expected_state_kinds = {
            item.name: item.kind
            for item in self.call.description.state_outputs
        }
        actual_state_kinds = {
            item.name: item.kind
            for item in scientific_return.states
        }
        if actual_state_kinds != expected_state_kinds:
            contract_failures.append(
                ValueError("Returned state differs from the call's declared outputs.")
            )
        output_types = {slot.description.name: slot.value_type for slot in self.call._output_slots}
        for returned_state in scientific_return.states:
            expected_type = output_types.get(returned_state.name)
            if expected_type is not None and type(returned_state.value) is not expected_type:
                contract_failures.append(TypeError(
                    f"State output {returned_state.name!r} requires {expected_type.__qualname__}; "
                    f"received {type(returned_state.value).__qualname__}"
                ))

        if contract_failures:
            cause = ExceptionGroup(
                "Scientific state return violates its binding contract.",
                contract_failures,
            )
            self.science.failure = cause
            self.science.resident.failure = cause
            adoption_receipt.set_exception(cause)
        else:
            adoption_receipt.set_result(None)


    async def observe(self, *, after, wait_seconds):
        await self.wait(after, wait_seconds)
        # silent-defaults: allow-contract -- Result adoption can precede thread completion; only a finished operation may expose its result reference.
        result = self.result if self.finished else None
        return Observation(
            operation=self.reference,
            target=self.invocation.target,
            revision=self.revision,
            acceptance=AcceptanceState.OWNED,
            dispatch=self.dispatch,
            finished=self.finished,
            outcome=self.outcome,
            result_state=self.result_state,
            result=result,
            states=self.returned_states,
            progress=tuple(self.progress),
            progress_dropped=self.progress_dropped,
            failures=self.failures,
            stop_requested=self.stop_requested,
            allowance_expired=self.expired,
            resources=self.resources,
        )

    def _require_result(self, result):
        self.require_loop()
        if type(result) is not ResultRef:
            raise TypeError(f"Unexpected {type(result)=}; result access requires a ResultRef")
        if not (
            result == self.result
            and self.finished
            and self.result_state is ResultState.AVAILABLE
        ):
            raise ValueError(
                "Result reference does not name this operation's available result."
            )

    async def read_result(self, result):
        self._require_result(result)
        if self.release is not None:
            raise ValueError(
                "Release of this exact result has already been authorized."
            )
        return self.payload

    def release_result(self, result):
        self.require_loop()
        # The exact release remains observable after its payload is discarded.
        # Check identity before returning it; a cached owner grants no authority
        # over a different operation or result.
        if self.release is not None and type(result) is ResultRef and result == self.result:
            return self.release
        self._require_result(result)
        if self.release is None:
            self.release = _Cleanup(self.invocation.policy.cleanup_overdue_flag_seconds)
            self.release.start(self._release)
        return self.release

    async def _release(self):
        if not self.value_disposed:
            disposal_future = self.science.resident.executor.submit(self.dispose_value)
            await await_original(disposal_future, deadline=None)

        self.payload = None
        self.result_state = ResultState.RELEASED
        self._release_capacity()
        self._forget_settled()
        self.changed()

    def _forget_settled(self):
        """Drop the host index only after execution and its owned values settle."""
        if (
            self.finished
            and (self.raw is None or self.value_disposed)
            and self.result_state is not ResultState.AVAILABLE
            and self.disposal_failure is None
        ):
            key = (self.analysis.identity, self.invocation.operation_id)
            if self.analysis.host.operations.get(key) is self:
                del self.analysis.host.operations[key]
            if (
                not self.retain_identity
                and self.state_allowance == 0
                and self.analysis.operations.get(self.invocation.operation_id) is self
            ):
                del self.analysis.operations[self.invocation.operation_id]

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
                raise TypeError(
                    "Malformed scientific return has no disposal contract; "
                    "its allocation remains owned."
                )
            if (
                type(self.raw) is ScientificReturn
                and self.raw.dispose_value is not None
            ):
                self.raw.dispose_value(self.raw.value)
        except BaseException as cause:
            self.disposal_failure = cause
            raise
        self.value_disposed = True
        self.raw = None
