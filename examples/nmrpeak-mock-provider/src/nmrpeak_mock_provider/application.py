"""Interpret one source, run scientific validation/generation and retain delivery.

Prediction is provider application code. It uses the SDK Interpreter to obtain
structured input, then submits the bound prepare and generate methods through
an EmbeddedAnalysis execution session. The SDK runs those methods; Prediction
retains their operation handles and chooses when generation can follow validation.
It retains one report receiver attempt; provider.py owns the request lifetime.
"""

from collections.abc import Awaitable, Callable

from provider_core.contribution_errors import CallRejected
from provider_core.conversation import ToolFinished, ToolRejected
from provider_core.embedded_host import EmbeddedAnalysis
from provider_core.hosting import (
    OperationPolicy, NotAdmitted, Outcome, ResultConsumption, ResultState, StartDeadlineExpired,
    observe_cleanup, observe_completion,
)
from provider_core.interpreter import Interpreter, ModelWindowExpired
from provider_core.input_tool import InputTool
from provider_core.scientific import HostedContribution

from .binding import PeakContribution
from .composition import interpretation_messages


class Prediction:
    """Coordinate one request's interpretation, scientific validation and generation.

    A successful prepare produces the input retained for generate. SDK admission
    alone does not establish that validation succeeded. Waiting again uses saved
    operation handles instead of submitting scientific work again.
    """

    def __init__(
        self, *, execution: EmbeddedAnalysis, hosted: HostedContribution,
        interpreter: Interpreter, policy: OperationPolicy,
        receive: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.execution = execution
        self.hosted = hosted
        self.interpreter = interpreter
        self.policy = policy
        self.input_tool = InputTool(
            call=hosted.call(method=hosted.contribution.prepare),
            description="Submit the formula and source-faithful NMR peaks for validation.",
        )
        self.receive = receive
        self.consumption: ResultConsumption | None = None
        self.preparations = []
        self.prepared = None
        self.generation = None

    async def dispatch(self, name: str, arguments: dict, *, not_after: float) -> ToolFinished:
        """Handle Interpreter's input proposal by submitting prepare to the SDK."""
        if self.prepared is not None:
            raise RuntimeError("This request already has an accepted interpretation")
        call = self.input_tool.call
        try:
            candidate = self.input_tool.decode(name=name, arguments=arguments)
        except CallRejected as cause:
            # Tell Interpreter which input the language model needs to correct.
            # Loading and execution failures propagate as their original exceptions.
            raise ToolRejected(cause.message) from cause
        reservation, = self.execution.reservations
        try:
            operation = self.execution.submit_method(
                method=self.hosted.contribution.prepare,
                scientific_input=candidate,
                states=(),
                reservation=reservation,
                policy=self.policy,
                start_not_after=not_after,
            )
            if isinstance(operation, NotAdmitted):
                raise RuntimeError(f"Cannot prepare scientific input: {operation=}")
            self.preparations.append(operation)
            completed = await observe_completion(operation=operation)
            if completed.outcome is Outcome.REJECTED:
                cause, = [failure.cause for failure in completed.failures]
                if isinstance(cause, CallRejected):
                    raise ToolRejected(cause.message) from cause
            self.require_result(completed=completed)
        except StartDeadlineExpired as cause:
            # Interpreter can change models only after the original SDK operation
            # has finished, or when the SDK refused it before admission.
            raise ModelWindowExpired() from cause
        self.prepared = await PeakContribution.validated.reference(call=call, operation=operation)
        # Successful prepare supplied the retained input needed by generate.
        # Release its report bytes; the SDK keeps the prepared input until generate
        # consumes it or analysis cleanup disposes it.
        await observe_cleanup(control=operation.release_result(result=completed.result))
        return ToolFinished(value="Scientific input validated")

    async def interpret(self, *, source: str) -> None:
        """Extract this offering's scientific input from the supplied source text.

        The request caller supplies source text. The selected scientific method
        already declares its input type; the SDK derives its tool schema.
        """
        await self.interpreter.run(
            messages=interpretation_messages(source=source),
            tools=[self.input_tool.definition],
            dispatch=self.dispatch,
        )

    async def deliver(self) -> None:
        """Generate once and join the original receiver attempt; never resend.

        A canceled wait may call deliver again on this request. A failed receiver
        keeps its original error and result available while the request stays open;
        the surrounding service owns reconciliation before closing the request.
        """
        if self.consumption is None:
            result = await self.finish()
            # Another waiter may have joined the same generation while this one
            # waited. Publish one receiver owner without an intervening await.
            if self.consumption is None:
                self.consumption = ResultConsumption(
                    operation=self.generation,
                    result=result,
                    receive=self.receive,
                )
        await self.consumption.consume()

    async def settle_receiver(self) -> None:
        """Join any started receiver before request cleanup can release its result."""
        if self.consumption is not None:
            await self.consumption.settle()

    async def finish(self):
        """Submit generate once, then wait on the same operation and return its result."""
        if self.prepared is None:
            raise RuntimeError("Accept an interpretation before generating candidates")
        if self.generation is None:
            reservation, = self.execution.reservations
            accepted = self.execution.submit_method(
                method=self.hosted.contribution.generate,
                states=(PeakContribution.candidate.bind(handle=self.prepared),),
                reservation=reservation, policy=self.policy,
            )
            if isinstance(accepted, NotAdmitted):
                raise RuntimeError(f"Cannot generate candidates: {accepted=}")
            self.generation = accepted
        completed = await observe_completion(operation=self.generation)
        self.require_result(completed=completed)
        return completed.result

    @staticmethod
    def require_result(*, completed) -> None:
        """Raise original failures, otherwise require a successful readable result."""
        causes = [failure.cause for failure in completed.failures]
        if len(causes) == 1:
            raise causes[0]
        if causes:
            raise BaseExceptionGroup("Independent scientific operation failures", causes)
        if completed.outcome is not Outcome.SUCCEEDED or completed.result_state is not ResultState.AVAILABLE:
            raise RuntimeError(f"Scientific operation has no consumable result: {completed=}")
