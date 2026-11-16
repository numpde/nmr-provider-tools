"""Run two sequential analyses on one local reference-index allocation."""

from __future__ import annotations

import asyncio
import sys

from provider_core.failure_collection import FailureCollector

from provider_core.hosting import (
    NotAdmitted,
    ResultConsumption,
    observe_cleanup,
    observe_completion,
)

from .provider import open_provider
from .science import VectorInput


async def run_demo() -> None:
    """Reuse initialized science; deliver prior reports before later failed cleanup.

    Query and analysis-cleanup observations keep their 15-second caller caps.
    Host exit then joins original retirement, deferring interruption while SDK
    resources are pending rather than abandoning cleanup when a cap expires.
    """

    async with open_provider() as provider:
        execution_host = provider.execution_host
        operation_policy = provider.operation_policy
        analysis_executor = None
        pending_report_deliveries = []
        failure_collector = FailureCollector(message="Reference matching and cleanup reported failures.")

        async def print_report(payload: bytes) -> None:
            # Receiver return means flushed local consumption, not durable delivery.
            sys.stdout.buffer.write(payload + b"\n")

            sys.stdout.buffer.flush()

        with failure_collector.capture():
            hosted_matcher = provider.matcher

            for values in ((3, 4, 0, 0), (0, 0, 1, 1)):
                analysis_executor = execution_host.open_analysis(allocations=frozenset({hosted_matcher.allocation}))
                (reservation_ref,) = analysis_executor.reservations

                match_operation = analysis_executor.submit_method(
                    method=hosted_matcher.contribution.match,
                    scientific_input=VectorInput(values=values),
                    states=(),
                    reservation=reservation_ref,
                    policy=operation_policy,
                )

                if isinstance(match_operation, NotAdmitted):
                    raise RuntimeError(f"Reference match admission refused: {match_operation.code.value=}; {match_operation.message=}")

                async with asyncio.timeout(15):
                    operation_observation = await observe_completion(operation=match_operation)

                pending_report_deliveries.append((match_operation, operation_observation.require_result()))

                # Same allocation supports sequential analyses. Its index remains
                # initialized; its first result remains retained under a separate owner.
                async with asyncio.timeout(15):
                    await observe_cleanup(control=analysis_executor.close())

                analysis_executor = None

        if analysis_executor is not None:
            with failure_collector.capture():
                analysis_executor.request_stop()

        # A later analysis failure must not suppress an earlier usable report.
        # Failed consumption keeps the result owned until this process exits.
        for (match_operation, result_ref) in pending_report_deliveries:
            with failure_collector.capture():
                async with ResultConsumption(operation=match_operation, result=result_ref, receive=print_report) as result_consumer:
                    await result_consumer.consume()

        # Completed sessions need no second observation. Keep only the
        # current session if its cleanup has not been confirmed yet.
        if analysis_executor is not None:
            with failure_collector.capture():
                async with asyncio.timeout(15):
                    await observe_cleanup(control=analysis_executor.close())

        failure_collector.raise_if_any()


if __name__ == "__main__":
    asyncio.run(run_demo())
