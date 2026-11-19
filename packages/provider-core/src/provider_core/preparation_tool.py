"""Turn a model's scientific-input proposal into one retained prepared state."""

from provider_core.container_guard import require_container

require_container()

import asyncio

from provider_core.contribution import CallBinding
from provider_core.contribution_errors import CallRejected
from provider_core.conversation import ToolFinished
from provider_core.conversation_errors import ToolRejected

from provider_core.hosting import (
    AnalysisExecution,
    Invocation,
    NotAdmitted,
    Observation,
    Operation,
    OperationPolicy,
    Outcome,
    ReservationRef,
    ResultRef,
    ResultRelease,
    StartDeadlineExpired,
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
        if not any((slot is state_output_slot) for slot in call_binding._output_slots):
            raise ValueError(f"One of {call_binding._output_slots=} should be {state_output_slot=}")

        if call_binding.description.state_inputs:
            raise ValueError(f"{call_binding.description.state_inputs=} should be empty")

        # Expose the selected call's input schema to the model and decode its proposals
        self.tool_definition = ScientificInputToolDefinition(call_binding=call_binding, description=description)

        self._proposal_lock = asyncio.Lock()  # Exclude concurrent dispatch and resume calls

        self.state_output_slot = state_output_slot  # Select the prepared state to retain from the operation
        self.analysis_executor = analysis_executor  # Submit preparation within the caller's analysis
        self.reservation_ref = reservation_ref  # Identify the backend assignment used for preparation
        self.operation_policy = operation_policy  # Apply execution deadlines, cleanup thresholds and payload limits

        # Retain the request and acquired operation so resume does not create new work
        self.preparation_submission: Submission | None = None

        # Publish the accepted state before report cleanup, preventing another proposal
        self.prepared_state_handle: StateHandle[PreparedT] | None = None

        # Remember the temporary report so an interrupted waiter can finish releasing it
        self.preparation_report_ref: ResultRef | None = None

        # Keep observing the same release if cleanup is interrupted
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

        # Reject a second proposal while the first is running. If it waited for
        # the lock, it could start as soon as the first proposal was rejected,
        # before its caller had seen that rejection and chosen to try again
        # See PreparationToolTests.test_competing_proposal_is_not_queued_after_input_rejection
        # in tests/unit/normative/test_preparation_tool.py
        if self._proposal_lock.locked():
            raise RuntimeError(f"{self._proposal_lock=} should be unlocked")

        async with self._proposal_lock:
            return await self._submit_proposal(name=name, arguments=arguments, not_after=not_after)

    async def resume(self) -> ToolFinished:
        """Finish the original preparation after an interrupted wait or lost reply."""

        # Resume continues work after the previous caller has stopped waiting
        # Reject a second caller while someone is still handling that work
        if self._proposal_lock.locked():
            raise RuntimeError(f"{self._proposal_lock=} should be unlocked")

        async with self._proposal_lock:
            if self.invocation is None:
                raise RuntimeError(f"{self.invocation=} should be set before resuming preparation")

            return await self._complete()

    @staticmethod
    def _start_expired(*, operation_observation: Observation) -> bool:
        if (not operation_observation.finished) or (len(operation_observation.failures) != 1):
            return False

        (failure,) = operation_observation.failures

        return failure.phase == "start" and isinstance(failure.cause, StartDeadlineExpired)

    async def _submit_proposal(self, *, name: str, arguments: dict, not_after: float) -> ToolFinished:
        if not (self.prepared_state_handle is None):
            raise RuntimeError(f"{self.prepared_state_handle=} should be unset before proposing new input")

        if self.invocation is not None:
            if self.operation is None:
                raise RuntimeError(f"{self.operation=} should be set by resuming preparation delivery before proposing new input")

            previous_operation_observation = await self.operation.observe(after=None, wait_seconds=0)

            if not (
                previous_operation_observation.finished and
                ((previous_operation_observation.outcome is Outcome.REJECTED) or
                 self._start_expired(operation_observation=previous_operation_observation))
            ):
                raise RuntimeError(
                    f"{previous_operation_observation.finished=}, {previous_operation_observation.outcome=}, "
                    f"but preparation should have finished by rejecting input or expiring before science"
                )

        try:
            scientific_input = self.tool_definition.decode(name=name, arguments=arguments)
        except CallRejected as cause:
            raise ToolRejected(cause.message) from cause

        call_binding = self.tool_definition.call_binding

        self.preparation_submission = Submission.from_call(
            analysis_executor=self.analysis_executor,
            call_binding=call_binding,
            scientific_input=scientific_input,
            states={},
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
                raise RuntimeError(f"Cannot prepare scientific input: {preparation_admission=!s}")

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
                if not (operation_observation.outcome is Outcome.REJECTED):
                    raise

                raise ToolRejected(cause.message) from cause

        if self.prepared_state_handle is None:
            self.prepared_state_handle = await self.state_output_slot.reference(call=self.tool_definition.call_binding, operation=preparation_operation)

        # Retain the exact report before requesting release: a lost reply must
        # retry that release, even after the report's bytes are already gone.
        self.preparation_report_release = preparation_operation.release_result(result=self.preparation_report_ref)

        await observe_cleanup(control=self.preparation_report_release)

        return ToolFinished(value="Scientific input prepared")
