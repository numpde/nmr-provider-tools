"""Run two sequential analyses on one local reference-index allocation."""

from __future__ import annotations

import asyncio
import sys

from provider_core.failure_collection import FailureCollector
from provider_core.hosting import (
    NotAdmitted, ResultConsumption,
    observe_cleanup, observe_completion,
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
        host = provider.host
        operation_policy = provider.operation_policy
        analysis = None
        retained = []
        failures = FailureCollector(message="Reference matching and cleanup reported failures.")

        async def print_report(payload: bytes) -> None:
            # Receiver return means flushed local consumption, not durable delivery.
            sys.stdout.buffer.write(payload + b"\n")
            sys.stdout.buffer.flush()

        with failures.capture():
            binding = provider.matcher
            for values in ((3, 4, 0, 0), (0, 0, 1, 1)):
                analysis = host.open_analysis(allocations=frozenset({binding.allocation}))
                reservation, = analysis.reservations
                operation = analysis.submit_method(
                    method=binding.contribution.match,
                    scientific_input=VectorInput(values=values),
                    states=(), reservation=reservation, policy=operation_policy,
                )
                if isinstance(operation, NotAdmitted):
                    raise RuntimeError(f"Reference match admission refused: {operation.code.value}: {operation.message}")
                async with asyncio.timeout(15):
                    completed = await observe_completion(operation=operation)
                retained.append((operation, completed.require_result()))
                # Same allocation supports sequential analyses. Its index remains
                # initialized; its first result remains retained under a separate owner.
                async with asyncio.timeout(15):
                    await observe_cleanup(control=analysis.close())
                analysis = None

        if analysis is not None:
            with failures.capture():
                analysis.request_stop()
        # A later analysis failure must not suppress an earlier usable report.
        # Failed consumption keeps the result owned until this process exits.
        for operation, result in retained:
            with failures.capture():
                async with ResultConsumption(operation=operation, result=result, receive=print_report) as consumption:
                    await consumption.consume()
        # Completed sessions need no second observation. Keep only the
        # current session if its cleanup has not been confirmed yet.
        if analysis is not None:
            with failures.capture():
                async with asyncio.timeout(15):
                    await observe_cleanup(control=analysis.close())
        failures.raise_if_any()


if __name__ == "__main__":
    asyncio.run(run_demo())
