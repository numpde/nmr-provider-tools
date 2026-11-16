"""Interpret one source, run scientific validation/generation and retain delivery.

PredictionWorkflow is provider application code. It uses the SDK Interpreter to
obtain structured input, then submits prepare and generate through an
InProcessAnalysisExecutor. The SDK runs those methods; the workflow retains
Submission owners and chooses when generation can follow validation.
It retains one report receiver attempt; provider.py owns the request lifetime.
"""

from collections.abc import Awaitable, Callable
from importlib.resources import files

from provider_core.embedded_host import InProcessAnalysisExecutor

from provider_core.hosting import (
    OperationPolicy,
    NotAdmitted,
    ResultConsumption,
)

from provider_core.interpreter import Interpreter
from provider_core.preparation_tool import PreparationTool
from provider_core.scientific import HostedContribution
from provider_core.submission import Submission

from .binding import PeakContribution


class PredictionWorkflow:
    """Coordinate one request's interpretation, scientific validation and generation.

    A successful prepare produces the input retained for generate. SDK admission
    alone does not establish that validation succeeded. Waiting again uses saved
    operation handles instead of submitting scientific work again.
    """

    def __init__(
        self,
        *,
        analysis_executor: InProcessAnalysisExecutor,
        hosted_predictor: HostedContribution,
        interpreter: Interpreter,
        operation_policy: OperationPolicy,
        receive: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.analysis_executor = analysis_executor
        self.hosted_predictor = hosted_predictor
        self.interpreter = interpreter
        self.operation_policy = operation_policy

        reservation_ref = analysis_executor.reservation(allocation=hosted_predictor.allocation)

        self.preparation_tool = PreparationTool(
            call_binding=hosted_predictor.call(method=hosted_predictor.contribution.prepare),
            state_output_slot=PeakContribution.validated,
            analysis_executor=analysis_executor,
            reservation_ref=reservation_ref,
            operation_policy=operation_policy,
            description="Submit the formula and source-faithful NMR peaks for validation.",
        )

        self.receive = receive
        self.result_consumer: ResultConsumption | None = None
        self.generation_submission: Submission | None = None

    async def interpret(self, *, source: str) -> None:
        """Extract this offering's scientific input from the supplied source text.

        The request caller supplies source text. The selected scientific method
        already declares its input type; the SDK derives its tool schema.
        Rejected proposals can be corrected; scientific acceptance retains input
        for generation.
        """
        prompt_files = files(anchor=__package__).joinpath("prompts")
        system_prompt = prompt_files.joinpath("system_prompt.txt").read_text(encoding="utf-8")

        offering_prompt = (
            prompt_files
            .joinpath(self.hosted_predictor.contribution.interpretation_prompt_file)
            .read_text(encoding="utf-8")
        )

        # The model calls prepare to propose a formula and peaks. The SDK also
        # supplies tools for inspecting input constraints and previous attempts.
        # The interpreter passes each proposal to dispatch for validation.
        # The model receives feedback on rejection and can try again.
        # After preparation accepts input, the SDK retains it for generate
        # and interpretation ends. Other failures propagate to the caller.
        # The SDK's interpreter.MODEL_TURNS and MODEL_REJECTIONS currently allow
        # 12 turns and 3 corrections per model; the deployment also sets a timeout.
        await self.interpreter.run(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": offering_prompt},
                {"role": "user", "content": source},
            ],
            tools=[self.preparation_tool.definition],
            dispatch=self.preparation_tool.dispatch,
        )

    async def deliver(self) -> None:
        """Generate once and join the original receiver attempt; never resend.

        A canceled wait may call deliver again on this request. A failed receiver
        keeps its original error and result available while the request stays open;
        the surrounding service owns reconciliation before closing the request.
        """

        if self.result_consumer is None:
            result_ref = await self.finish()

            # Another waiter may have joined the same generation while this one
            # waited. Publish one receiver owner without an intervening await.
            if self.result_consumer is None:
                self.result_consumer = ResultConsumption(
                    operation=self.generation_submission.operation,
                    result=result_ref,
                    receive=self.receive,
                )

        await self.result_consumer.consume()

    async def settle_receiver(self) -> None:
        """Join any started receiver before request cleanup can release its result."""

        if self.result_consumer is not None:
            await self.result_consumer.settle()

    async def finish(self):
        """Return the generated result reference after preparation is accepted.

        Missing prepared input or an admission refusal raises. Later waits reuse
        the original submitted operation.
        """

        prepared_state_handle = self.preparation_tool.prepared_state_handle

        if prepared_state_handle is None:
            raise RuntimeError("Accept an interpretation before generating candidates")

        generation_submission = self.generation_submission

        if generation_submission is None:
            reservation_ref = self.analysis_executor.reservation(allocation=self.hosted_predictor.allocation)

            generation_submission = Submission.from_call(
                analysis_executor=self.analysis_executor,
                call_binding=self.hosted_predictor.call(method=self.hosted_predictor.contribution.generate),
                states={PeakContribution.candidate: prepared_state_handle},
                reservation_ref=reservation_ref,
                operation_policy=self.operation_policy,
            )

            self.generation_submission = generation_submission

        if isinstance((generation_admission := generation_submission.submit()), NotAdmitted):
            raise RuntimeError(f"Cannot generate candidates: {generation_admission=}")

        operation_observation = await generation_submission.wait()

        return operation_observation.require_result()
