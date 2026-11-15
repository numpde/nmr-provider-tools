"""Turn a model's scientific-input proposal into one retained prepared state."""

from provider_core.container_guard import require_container

require_container()

import asyncio

from provider_core.contribution import CallBinding, ref
from provider_core.contribution_errors import CallRejected
from provider_core.conversation import ToolFinished, ToolRejected
from provider_core.hosting import (
    AnalysisExecution, Invocation, NotAdmitted, Observation, Operation, OperationPolicy,
    Outcome, ReservationRef, ResultRef, ResultRelease, StartDeadlineExpired,
    observe_cleanup, observe_completion,
)
from provider_core.input_tool import InputTool
from provider_core.interpreter import ModelWindowExpired
from provider_core.state_slots import StateHandle, StateOutputSlot


class PreparationTool[PreparedT]:
    """Accept scientific input through Interpreter and retain its prepared state.

    The provider selects the scientific call and its output port. The SDK derives
    the model tool, runs preparation and releases its temporary report. Only input
    rejection asks the model to correct its proposal; operator failures propagate.

    ``operation`` retains the last admitted attempt, including after cancellation.
    Only rejection or a confirmed start-window expiry permits another proposal.
    ``prepared`` is published before report release, so interrupted cleanup cannot
    authorize new preparation.
    resume() joins interrupted work or retries delivery of the original request;
    it never creates another preparation. The analysis owns operations and state
    cleanup; this tool does not retire it.
    """

    def __init__(
        self,
        *,
        call: CallBinding,
        output: StateOutputSlot[PreparedT],
        execution: AnalysisExecution,
        reservation: ReservationRef,
        policy: OperationPolicy,
        description: str,
    ) -> None:
        if not any(slot is output for slot in call._output_slots):
            raise ValueError("Cannot prepare input with an output port not declared by the selected call")
        if call.description.state_inputs:
            raise ValueError("A preparation tool accepts scientific input without existing state inputs")
        self._dispatch_lock = asyncio.Lock()
        self.input_tool = InputTool(call=call, description=description)
        self.output = output
        self.execution = execution
        self.reservation = reservation
        self.policy = policy
        self.invocation: Invocation | None = None
        self.operation: Operation | None = None
        self.prepared: StateHandle[PreparedT] | None = None
        self.report: ResultRef | None = None
        self.report_release: ResultRelease | None = None

    @property
    def name(self) -> str:
        return self.input_tool.name

    @property
    def definition(self) -> dict:
        return self.input_tool.definition

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
    def _start_expired(*, observation: Observation) -> bool:
        if not observation.finished or len(observation.failures) != 1:
            return False
        failure, = observation.failures
        return failure.phase == "start" and isinstance(failure.cause, StartDeadlineExpired)

    async def _prepare(self, *, name: str, arguments: dict, not_after: float) -> ToolFinished:
        if self.prepared is not None:
            raise RuntimeError("This tool already has accepted scientific input")
        if self.invocation is not None:
            if self.operation is None:
                raise RuntimeError("Resume delivery of the original preparation before proposing new input")
            previous = await self.operation.observe(after=None, wait_seconds=0)
            if not previous.finished or (
                previous.outcome is not Outcome.REJECTED and not self._start_expired(observation=previous)
            ):
                raise RuntimeError("Resume the original preparation; it has not rejected input or expired before science")
        try:
            candidate = self.input_tool.decode(name=name, arguments=arguments)
        except CallRejected as cause:
            raise ToolRejected(cause.message) from cause
        call = self.input_tool.call
        self.invocation = Invocation(
            target=ref(call=call),
            payload=call.input_codec.encode(value=candidate),
            states=(),
            reservation=self.reservation,
            policy=self.policy,
            start_not_after=not_after,
        )
        self.operation = None
        return await self._complete()

    async def _complete(self) -> ToolFinished:
        if self.report_release is not None:
            await observe_cleanup(control=self.report_release)
            return ToolFinished(value="Scientific input prepared")
        if self.operation is None:
            try:
                operation = self.execution.submit(invocation=self.invocation)
            except StartDeadlineExpired as cause:
                # Submission rejected this request before acquiring an operation.
                self.invocation = None
                raise ModelWindowExpired() from cause
            if isinstance(operation, NotAdmitted):
                raise RuntimeError(f"Cannot prepare scientific input: {operation=}")
            self.operation = operation
        operation = self.operation
        if self.report is None:
            completed = await observe_completion(operation=operation)
            if self._start_expired(observation=completed):
                # Loading has settled and the backend confirms science never started.
                raise ModelWindowExpired() from completed.failures[0].cause
            try:
                self.report = completed.require_result()
            except CallRejected as cause:
                # Encoding can also raise CallRejected after science succeeded.
                # Only the scientific rejection outcome permits model correction.
                if completed.outcome is not Outcome.REJECTED:
                    raise
                raise ToolRejected(cause.message) from cause
        if self.prepared is None:
            self.prepared = await self.output.reference(call=self.input_tool.call, operation=operation)
        # Retain the exact report before requesting release: a lost reply must
        # retry that release, even after the report's bytes are already gone.
        self.report_release = operation.release_result(result=self.report)
        await observe_cleanup(control=self.report_release)
        return ToolFinished(value="Scientific input prepared")
