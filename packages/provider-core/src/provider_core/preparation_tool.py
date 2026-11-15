"""Turn a model's scientific-input proposal into one retained prepared state."""

from provider_core.container_guard import require_container

require_container()

import asyncio

from provider_core.contribution import CallBinding
from provider_core.contribution_errors import CallRejected
from provider_core.conversation import ToolFinished, ToolRejected

from provider_core.hosting import (
    AnalysisExecution, Invocation, NotAdmitted, Observation, Operation, OperationPolicy,
    Outcome, ReservationRef, ResultRef, ResultRelease, StartDeadlineExpired,
    observe_cleanup,
)

from provider_core.input_tool import ScientificInputToolDefinition
from provider_core.interpreter import ModelWindowExpired
from provider_core.state_slots import StateHandle, StateOutputSlot
from provider_core.submission import Submission


class PreparationTool[PreparedT]:
    """Accept scientific input through Interpreter and retain its prepared state.

    The provider selects the scientific call and its output port. The SDK derives
    the model tool, runs preparation and releases its temporary report. Only input
    rejection asks the model to correct its proposal; operator failures propagate.

    ``operation`` retains the last admitted attempt, including after cancellation.
    Only rejection or a confirmed start-window expiry permits another proposal.
    ``prepared_state_handle`` is published before report release, so interrupted
    cleanup cannot authorize new preparation.
    resume() joins interrupted work or retries delivery of the original request;
    it never creates another preparation. The analysis owns operations and state
    cleanup; this tool does not retire it.
    """

    def __init__(
        self,
        *,
        call_binding: CallBinding,
        state_output_slot: StateOutputSlot[PreparedT],
        analysis_executor: AnalysisExecution,
        reservation_ref: ReservationRef,
        operation_policy: OperationPolicy,
        description: str,
    ) -> None:
        if not any(slot is state_output_slot for slot in call_binding._output_slots):
            raise ValueError("Cannot prepare input with an output port not declared by the selected call")

        if call_binding.description.state_inputs:
            raise ValueError("A preparation tool accepts scientific input without existing state inputs")

        self._dispatch_lock = asyncio.Lock()
        self.tool_definition = ScientificInputToolDefinition(call_binding=call_binding, description=description)
        self.state_output_slot = state_output_slot
        self.analysis_executor = analysis_executor
        self.reservation_ref = reservation_ref
        self.operation_policy = operation_policy
        self.preparation_submission: Submission | None = None
        self.prepared_state_handle: StateHandle[PreparedT] | None = None
        self.preparation_report_ref: ResultRef | None = None
        self.preparation_report_release: ResultRelease | None = None

    @property
    def invocation(self) -> Invocation | None:
        if self.preparation_submission is None:
            return None

        return self.preparation_submission.invocation

    @property
    def operation(self) -> Operation | None:
        if self.preparation_submission is None:
            return None

        return self.preparation_submission.operation

    @property
    def name(self) -> str:
        return self.tool_definition.name

    @property
    def definition(self) -> dict:
        return self.tool_definition.definition

    async def dispatch(self, name: str, arguments: dict, *, not_after: float) -> ToolFinished:
        """Run one proposal; keep original work observable when its waiter exits."""

        if self._dispatch_lock.locked():
            raise RuntimeError("Another proposal is already being prepared by this tool")

        async with self._dispatch_lock:
            return await self._prepare(name=name, arguments=arguments, not_after=not_after)

    async def resume(self) -> ToolFinished:
        """Finish the original preparation after an interrupted wait or lost reply."""

        if self._dispatch_lock.locked():
            raise RuntimeError("Another waiter is already observing preparation through this tool")

        async with self._dispatch_lock:
            if self.invocation is None:
                raise RuntimeError("Submit scientific input before resuming preparation")

            return await self._complete()

    @staticmethod
    def _start_expired(*, operation_observation: Observation) -> bool:
        if not operation_observation.finished or len(operation_observation.failures) != 1:
            return False

        (failure,) = operation_observation.failures

        return failure.phase == "start" and isinstance(failure.cause, StartDeadlineExpired)

    async def _prepare(self, *, name: str, arguments: dict, not_after: float) -> ToolFinished:
        if self.prepared_state_handle is not None:
            raise RuntimeError("This tool already has accepted scientific input")

        if self.invocation is not None:
            if self.operation is None:
                raise RuntimeError("Resume delivery of the original preparation before proposing new input")

            previous_operation_observation = await self.operation.observe(after=None, wait_seconds=0)

            if not previous_operation_observation.finished or (
                previous_operation_observation.outcome is not Outcome.REJECTED and not self._start_expired(operation_observation=previous_operation_observation)
            ):
                raise RuntimeError("Resume the original preparation; it has not rejected input or expired before science")

        try:
            scientific_input = self.tool_definition.decode(name=name, arguments=arguments)
        except CallRejected as cause:
            raise ToolRejected(cause.message) from cause

        call_binding = self.tool_definition.call_binding

        self.preparation_submission = Submission.from_call(
            analysis_executor=self.analysis_executor,
            call_binding=call_binding,
            scientific_input=scientific_input,
            states=(),
            reservation_ref=self.reservation_ref,
            operation_policy=self.operation_policy,
            start_not_after=not_after,
        )

        return await self._complete()

    async def _complete(self) -> ToolFinished:
        if self.preparation_report_release is not None:
            await observe_cleanup(control=self.preparation_report_release)

            return ToolFinished(value="Scientific input prepared")

        if self.operation is None:
            try:
                preparation_admission = self.preparation_submission.submit()
            except StartDeadlineExpired as cause:
                # Submission rejected this request before acquiring an operation.
                self.preparation_submission = None

                raise ModelWindowExpired() from cause

            if isinstance(preparation_admission, NotAdmitted):
                raise RuntimeError(f"Cannot prepare scientific input: {preparation_admission=}")

        preparation_operation = self.operation

        if self.preparation_report_ref is None:
            operation_observation = await self.preparation_submission.wait()

            if self._start_expired(operation_observation=operation_observation):
                # Loading has settled and the backend confirms science never started.
                raise ModelWindowExpired() from operation_observation.failures[0].cause

            try:
                self.preparation_report_ref = operation_observation.require_result()
            except CallRejected as cause:
                # Encoding can also raise CallRejected after science succeeded.
                # Only the scientific rejection outcome permits model correction.
                if operation_observation.outcome is not Outcome.REJECTED:
                    raise

                raise ToolRejected(cause.message) from cause

        if self.prepared_state_handle is None:
            self.prepared_state_handle = await self.state_output_slot.reference(call=self.tool_definition.call_binding, operation=preparation_operation)

        # Retain the exact report before requesting release: a lost reply must
        # retry that release, even after the report's bytes are already gone.
        self.preparation_report_release = preparation_operation.release_result(result=self.preparation_report_ref)

        await observe_cleanup(control=self.preparation_report_release)

        return ToolFinished(value="Scientific input prepared")
