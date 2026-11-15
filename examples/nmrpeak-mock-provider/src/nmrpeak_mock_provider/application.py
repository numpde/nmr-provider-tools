"""Interpret one source, run scientific validation/generation and retain delivery.

Prediction is provider application code. It uses the SDK Interpreter to obtain
structured input, then submits the bound prepare and generate methods through
an EmbeddedAnalysis execution session. The SDK runs those methods; Prediction
retains SDK Submission owners and chooses when generation can follow validation.
It retains one report receiver attempt; provider.py owns the request lifetime.
"""

from collections.abc import Awaitable, Callable
from importlib.resources import files

from provider_core.embedded_host import EmbeddedAnalysis
from provider_core.hosting import (
    OperationPolicy, NotAdmitted, ResultConsumption,
)
from provider_core.interpreter import Interpreter
from provider_core.preparation_tool import PreparationTool
from provider_core.scientific import HostedContribution
from provider_core.submission import Submission

from .binding import PeakContribution


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
        reservation, = execution.reservations
        self.preparation = PreparationTool(
            call=hosted.call(method=hosted.contribution.prepare),
            output=PeakContribution.validated,
            execution=execution,
            reservation=reservation,
            policy=policy,
            description="Submit the formula and source-faithful NMR peaks for validation.",
        )
        self.receive = receive
        self.consumption: ResultConsumption | None = None
        self.generation: Submission | None = None

    async def interpret(self, *, source: str) -> None:
        """Extract this offering's scientific input from the supplied source text.

        The request caller supplies source text. The selected scientific method
        already declares its input type; the SDK derives its tool schema.
        """
        instructions = files(anchor=__package__).joinpath("interpreter.md").read_text(encoding="utf-8")
        await self.interpreter.run(
            messages=[{"role": "system", "content": instructions}, {"role": "user", "content": source}],
            tools=[self.preparation.definition],
            dispatch=self.preparation.dispatch,
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
                    operation=self.generation.operation,
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
        if self.preparation.prepared is None:
            raise RuntimeError("Accept an interpretation before generating candidates")
        if self.generation is None:
            reservation, = self.execution.reservations
            self.generation = Submission.from_call(
                execution=self.execution,
                call=self.hosted.call(method=self.hosted.contribution.generate),
                states=(PeakContribution.candidate.bind(handle=self.preparation.prepared),),
                reservation=reservation, policy=self.policy,
            )
        accepted = self.generation.submit()
        if isinstance(accepted, NotAdmitted):
            raise RuntimeError(f"Cannot generate candidates: {accepted=}")
        completed = await self.generation.wait()
        return completed.require_result()
