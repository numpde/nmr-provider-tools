"""Interpret, validate and generate one selected NMRPeak request.

Job discovery and durable API publication surround this boundary. The SDK owns
the conversation, execution and result-consumption lifetimes; this application
chooses which scientific method follows an accepted interpretation.
"""

import asyncio
import json

from provider_core.contribution_errors import CallRejected
from provider_core.conversation import ToolFinished, ToolRejected
from provider_core.embedded_host import EmbeddedAnalysis
from provider_core.hosting import (
    ExecutionLimits, NotAdmitted, Outcome, ResultState,
    observe_cleanup, observe_completion,
)
from provider_core.interpreter import Interpreter, ModelWindowExpired
from provider_core.scientific import HostedContribution

from .binding import PeakContribution


class Prediction:
    """Retain accepted preparation and generation; waiting never resubmits them."""

    def __init__(
        self, *, execution: EmbeddedAnalysis, hosted: HostedContribution,
        interpreter: Interpreter, limits: ExecutionLimits, require_permission,
    ) -> None:
        self.execution = execution
        self.hosted = hosted
        self.interpreter = interpreter
        self.limits = limits
        self.require_permission = require_permission
        self.preparations = []
        self.prepared = None
        self.generation = None

    async def dispatch(self, name: str, arguments: dict, *, not_after: float) -> ToolFinished:
        if name != "submit_interpretation" or set(arguments) != {"value"}:
            raise ToolRejected("Submit the complete scientific input under value")
        if self.prepared is not None:
            raise RuntimeError("This request already has an accepted interpretation")
        contribution = self.hosted.contribution
        call = self.hosted.call(method=contribution.prepare)
        try:
            candidate = call.input_codec.decode(payload=json.dumps(obj=arguments["value"]).encode(encoding="utf-8"))
        except CallRejected as cause:
            # Scientific input corrections are model-visible. Operational errors
            # bypass this translation and retain their original exception.
            raise ToolRejected(cause.message) from cause
        loop = asyncio.get_running_loop()

        def permit_validation():
            self.require_permission()
            if not (loop.time() < not_after):
                raise ModelWindowExpired()

        # Parsing and queueing spend the same model window. Recheck both before
        # admission and at actual dispatch, without renewing that deadline.
        permit_validation()
        reservation, = self.execution.reservations
        operation = self.execution.submit_method(
            method=contribution.prepare, operation_id=str(len(self.preparations)),
            scientific_input=candidate, states=(), reservation=reservation,
            limits=self.limits, dispatch_guard=permit_validation,
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
        self.prepared = await PeakContribution.prepared_output.reference(call=call, operation=operation)
        # The validation report has served its consumer. Its separate live state
        # remains owned by this analysis until generation consumes it.
        await observe_cleanup(control=operation.release_result(result=completed.result))
        return ToolFinished(value="Scientific input validated")

    async def interpret(self, *, messages: list[dict], tools: list[dict]) -> None:
        await self.interpreter.run(
            messages=messages, tools=tools, dispatch=self.dispatch,
        )

    async def finish(self):
        if self.prepared is None:
            raise RuntimeError("Accept an interpretation before generating candidates")
        if self.generation is None:
            reservation, = self.execution.reservations
            accepted = self.execution.submit_method(
                method=self.hosted.contribution.generate, operation_id="generate",
                states=(PeakContribution.prepared_input.bind(handle=self.prepared),),
                reservation=reservation, limits=self.limits,
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
        causes = [failure.cause for failure in completed.failures]
        if len(causes) == 1:
            raise causes[0]
        if causes:
            raise BaseExceptionGroup("Independent scientific operation failures", causes)
        if completed.outcome is not Outcome.SUCCEEDED or completed.result_state is not ResultState.AVAILABLE:
            raise RuntimeError(f"Scientific operation has no consumable result: {completed=}")
