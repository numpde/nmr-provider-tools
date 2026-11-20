"""Sequence queries and deliver retained earlier reports after later failure."""

from __future__ import annotations

import asyncio

from provider_core.failure_collection import FailureCollector

from provider_core.submission import Submission

from provider_core.hosting import (
    NotAdmitted,
    ResultConsumption,
    observe_cleanup,
)

async def run_queries(*, provider, candidates, receive) -> None:
    """Reuse initialized science; deliver prior reports before later failed cleanup.

    Query and analysis-cleanup observations keep their 15-second caller caps.
    Host exit then joins original retirement, deferring interruption while SDK
    resources are pending rather than abandoning cleanup when a cap expires.
    """

    execution_host = provider.execution_host
    operation_policy = provider.operation_policy
    analysis_executor = None
    pending_report_deliveries = []
    failure_collector = FailureCollector(message="Reference matching and cleanup reported failures.")

    with failure_collector.capture():
        hosted_matcher = provider.matcher

        for candidate in candidates:
            analysis_executor = execution_host.open_analysis(allocations=frozenset({hosted_matcher.allocation}))
            reservation_ref = analysis_executor.reservation(allocation=hosted_matcher.allocation)

            submission = Submission.from_call(
                analysis_executor=analysis_executor,
                call_binding=provider.match_call,
                scientific_input=candidate,
                states={},
                reservation_ref=reservation_ref,
                operation_policy=operation_policy,
            )
            match_operation = submission.submit()

            if isinstance(match_operation, NotAdmitted):
                raise RuntimeError(f"Reference match admission refused: {match_operation=!s}")

            async with asyncio.timeout(15):
                operation_observation = await submission.wait()

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
    # Failed consumption keeps the result owned until SDK host retirement.
    for (match_operation, result_ref) in pending_report_deliveries:
        with failure_collector.capture():
            async with ResultConsumption(operation=match_operation, result=result_ref, receive=receive) as result_consumer:
                await result_consumer.consume()

    # Completed sessions need no second observation. Keep only the
    # current session if its cleanup has not been confirmed yet.
    if analysis_executor is not None:
        with failure_collector.capture():
            async with asyncio.timeout(15):
                await observe_cleanup(control=analysis_executor.close())

    failure_collector.raise_if_any()

