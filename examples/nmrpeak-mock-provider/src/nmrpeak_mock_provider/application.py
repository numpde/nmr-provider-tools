"""Ask the SDK to validate interpreted input and run scientific generation.

Prediction is provider application code. It uses the SDK Interpreter to obtain
structured input, then submits the bound prepare and generate methods through
an EmbeddedAnalysis execution session. The SDK runs those methods; Prediction
retains their operation handles and chooses when generation can follow validation.
"""

import asyncio
import json

from provider_core.contribution_errors import CallRejected
from provider_core.conversation import ToolFinished, ToolRejected
from provider_core.embedded_host import EmbeddedAnalysis
from provider_core.hosting import (
    OperationPolicy, NotAdmitted, Outcome, ResultState,
    observe_cleanup, observe_completion,
)
from provider_core.interpreter import Interpreter, ModelWindowExpired
from provider_core.scientific import HostedContribution

from .binding import PeakContribution


class Prediction:
    """Coordinate one request's interpretation, scientific validation and generation.

    A successful prepare produces the input retained for generate. SDK admission
    alone does not establish that validation succeeded. Waiting again uses saved
    operation handles instead of submitting scientific work again.
    """

    def __init__(
        self, *, execution: EmbeddedAnalysis, hosted: HostedContribution,
        interpreter: Interpreter, policy: OperationPolicy, require_permission,
    ) -> None:
        self.execution = execution
        self.hosted = hosted
        self.interpreter = interpreter
        self.policy = policy
        self.require_permission = require_permission
        self.preparations = []
        self.prepared = None
        self.generation = None

    async def dispatch(self, name: str, arguments: dict, *, not_after: float) -> ToolFinished:
        """Handle Interpreter's input proposal by submitting prepare to the SDK."""
        if name != "submit_interpretation" or set(arguments) != {"value"}:
            raise ToolRejected("Submit the complete scientific input under value")
        if self.prepared is not None:
            raise RuntimeError("This request already has an accepted interpretation")
        contribution = self.hosted.contribution
        call = self.hosted.call(method=contribution.prepare)
        try:
            candidate = call.input_codec.decode(payload=json.dumps(obj=arguments["value"]).encode(encoding="utf-8"))
        except CallRejected as cause:
            # Tell Interpreter which input the language model needs to correct.
            # Loading and execution failures propagate as their original exceptions.
            raise ToolRejected(cause.message) from cause
        loop = asyncio.get_running_loop()

        def permit_validation():
            self.require_permission()
            if not (loop.time() < not_after):
                raise ModelWindowExpired()

        # Interpreter supplies one deadline for proposing and validating input.
        # Check it before submission and let the SDK recheck it when prepare can
        # start. Time spent parsing or waiting does not extend that deadline.
        permit_validation()
        reservation, = self.execution.reservations
        operation = self.execution.submit_method(
            method=contribution.prepare,
            scientific_input=candidate, states=(), reservation=reservation,
            policy=self.policy, dispatch_guard=permit_validation,
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
        self.prepared = await PeakContribution.validated.reference(call=call, operation=operation)
        # Successful prepare supplied the retained input needed by generate.
        # Release its report bytes; the SDK keeps the prepared input until generate
        # consumes it or analysis cleanup disposes it.
        await observe_cleanup(control=operation.release_result(result=completed.result))
        return ToolFinished(value="Scientific input validated")

    async def interpret(self, *, messages: list[dict], tools: list[dict]) -> None:
        """Ask Interpreter for structured input, using dispatch for its tool proposals."""
        await self.interpreter.run(
            messages=messages, tools=tools, dispatch=self.dispatch,
        )

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
                dispatch_guard=self.require_permission,
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
