"""Retain one scientific request before delivery can have an uncertain outcome."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import Mapping

from provider_core.contribution import CallBinding, NoInput, ref

from provider_core.hosting import (
    AnalysisExecution,
    Invocation,
    NotAdmitted,
    Observation,
    Operation,
    OperationPolicy,
    ReservationRef,
    observe_completion,
)

from provider_core.state_slots import StateHandle, StateInputSlot, state_references


class Submission:
    """Own one invocation and any operation acquired by delivering it.

    Construction has no execution effects. Each explicit submit() delivers the
    same invocation until an operation is returned, then returns that operation
    without delivery. Refusals and backend exceptions propagate unchanged; the
    caller decides whether another delivery attempt is appropriate.

    wait() only observes acquired work. Cancellation does not stop that work,
    and later waits read current observations rather than cache a prior success.
    Results, receivers and analysis cleanup retain their separate owners.
    Portable request deduplication lives until analysis closure; this does not
    change submit_method(), where every call requests new work.
    """

    def __init__(self, *, analysis_executor: AnalysisExecution, invocation: Invocation) -> None:
        self._analysis_executor = analysis_executor
        self._invocation = invocation
        self._operation: Operation | None = None

    @property
    def invocation(self) -> Invocation:
        return self._invocation

    @property
    def operation(self) -> Operation | None:
        return self._operation

    @classmethod
    # silent-defaults: allow-contract -- NoInput represents absent payload; the call codec rejects absence when input is required.
    def from_call[InputT, StateT, OutputT](
        cls,
        *,
        analysis_executor: AnalysisExecution,
        call_binding: CallBinding[InputT, StateT, OutputT],
        scientific_input: InputT | NoInput = NoInput(),
        states: Mapping[StateInputSlot, StateHandle],
        reservation_ref: ReservationRef,
        operation_policy: OperationPolicy,
        start_not_after: float | None = None,
    ) -> Submission:
        """Encode input and project state-port handles without submitting."""

        return cls(
            analysis_executor=analysis_executor,
            invocation=Invocation(
                target=ref(call=call_binding),
                payload=call_binding.encode_input(value=scientific_input),
                states=state_references(states=states),
                reservation=reservation_ref,
                policy=operation_policy,
                start_not_after=start_not_after,
            ),
        )

    def submit(self) -> Operation | NotAdmitted:
        """Deliver this request once, or return its already acquired operation."""

        if self.operation is not None:
            return self.operation

        admission_result = self._analysis_executor.submit(invocation=self.invocation)

        if not isinstance(admission_result, NotAdmitted):
            self._operation = admission_result

        return admission_result

    async def wait(self) -> Observation:
        """Observe original admitted work without submitting or releasing anything."""

        if self.operation is None:
            raise RuntimeError(f"{self.operation=} should be set")

        return await observe_completion(operation=self.operation)
