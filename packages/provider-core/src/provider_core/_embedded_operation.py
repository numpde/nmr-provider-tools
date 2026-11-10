"""One owned calculation in the bounded local execution backend."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import threading
from concurrent.futures import Future
from uuid import uuid4

from provider_core._embedded_support import _Changes, _Cleanup, _State
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
    StateArgument,
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
    def __init__(self, analysis, science, invocation, call, guard, states):
        super().__init__()
        self.analysis = analysis
        self.science = science
        self.invocation = invocation
        self.call = call
        self.guard = guard
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
        self.result_allowance = invocation.limits.result_bytes
        self.state_allowance = len(call.description.state_outputs)
        self.execution_timer = None
        self.resources = ResourceState.RESERVED
        self.control = _OperationControl(self)

    def start(self):
        self.queue_timer = self.loop.call_later(
            self.invocation.limits.queue_seconds,
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
        self.require_loop()
        if not self.stop_requested:
            self.stop_requested = True
            self.stop_signal.set()
            self.changed()

    def _guard_at_handoff(self):
        permit = Future()
        self.loop.call_soon_threadsafe(self._admit_dispatch, permit)
        permit.result()

    def _admit_dispatch(self, permit):
        try:
            if self.analysis.closed or self.stop_requested:
                raise PermissionError(
                    "This operation was stopped before its next scientific entry."
                )
            self.guard()
            if self.dispatch is DispatchState.QUEUED:
                self.dispatch = DispatchState.STARTED
                self.resources = ResourceState.OCCUPIED
                self.queue_timer.cancel()
                self.execution_timer = self.loop.call_later(
                    self.invocation.limits.execution_seconds,
                    self._expire,
                )
                # Consume state only after the guarded scientific dispatch begins.
                for parameter in self.call.description.state_inputs:
                    if parameter.use is StateUse.CONSUME:
                        self.state_owners[parameter.name].available = False
            self.changed()
        except BaseException as cause:
            permit.set_exception(cause)
        else:
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
            if not (
                type(result_payload) is bytes
                and len(result_payload) <= self.invocation.limits.result_bytes
            ):
                raise ValueError(
                    "Encoded scientific result exceeds its declared byte allowance "
                    "or is not bytes."
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
                self.analysis.host.limits.initialization_seconds
            )
        finally:
            self.loop.call_soon_threadsafe(initialization_timer.cancel)

    def _start_initialization_clock(self, timer_receipt):
        timer_receipt.set_result(
            self.loop.call_later(
                self.analysis.host.limits.initialization_seconds,
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
            if len(self.progress) == self.analysis.host.limits.progress_records:
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
            self.analysis.host.state_reserved -= self.state_allowance
            self.state_allowance = 0
            self._release_capacity()

        self.finished = True
        self.completion.set()
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
            )
            self.analysis.states[state_reference] = _State(returned_state, self)
            returned_arguments.append(StateArgument(returned_state.name, state_reference))

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
        result = self.result if self.finished else None
        return Observation(
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
        if not (
            type(result) is ResultRef
            and result == self.result
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
        self._require_result(result)
        if self.release is None:
            self.release = _Cleanup(self.invocation.limits.cleanup_seconds)
            self.release.start(self._release)
        return self.release

    async def _release(self):
        if not self.value_disposed:
            disposal_future = self.science.resident.executor.submit(self.dispose_value)
            await await_original(disposal_future, deadline=None)

        self.payload = None
        self.result_state = ResultState.RELEASED
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
