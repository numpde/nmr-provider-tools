"""Run two sequential analyses on one local reference-index allocation."""

from __future__ import annotations

import asyncio
import sys

from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import (
    OperationPolicy, NotAdmitted, Outcome, ResultConsumption, ResultState,
    observe_cleanup, observe_completion,
)

from .binding import ReferenceMatcher, create_binding
from .science import VectorInput


async def run_demo() -> None:
    """Reuse initialized science; retain results beyond each analysis lifetime."""
    host = EmbeddedHost(
        input_bytes=8192,
        result_bytes=8192,
        state_values=1,
        progress_records=1,
        initialization_seconds=5,
        retirement_seconds=5,
    )
    operation_policy = OperationPolicy(
        queue_stop_request_seconds=5,
        execution_stop_request_seconds=10,
        cleanup_overdue_flag_seconds=5,
        reject_encoded_input_over_bytes=4096,
        reject_encoded_result_over_bytes=4096,
    )
    analysis = None
    cleanup_requests = []
    retained = []
    causes = []


    async def print_report(payload: bytes) -> None:
        # Receiver return means flushed local consumption, not durable delivery.
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()

    try:
        binding = create_binding(host=host)
        for name, values in (("first", (3, 4, 0, 0)), ("second", (0, 0, 1, 1))):
            analysis = host.open_analysis(allocations=(binding.allocation,))
            cleanup_requests.append(analysis.close)
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
            if completed.failures:
                causes.extend(failure.cause for failure in completed.failures)
                break
            if completed.outcome is not Outcome.SUCCEEDED or completed.result_state is not ResultState.AVAILABLE:
                raise RuntimeError(f"Reference match produced no usable report: {completed!r}")
            retained.append((operation, completed.result))
            # Same allocation supports sequential analyses. Its index remains
            # initialized; its first result remains retained under a separate owner.
            async with asyncio.timeout(15):
                await observe_cleanup(control=analysis.close())

    except BaseException as cause:
        causes.append(cause)
    finally:
        if analysis is not None:
            analysis.request_stop()
        # A later analysis failure must not suppress an earlier usable report.
        # Failed consumption keeps the result owned until this process exits.
        for operation, result in retained:
            consumption = ResultConsumption(operation=operation, result=result, receive=print_report)
            try:
                await consumption.consume()
            except BaseException as cause:
                causes.append(cause)
            finally:
                # A cancelled observer must not abandon the original receiver
                # while later analysis/host cleanup invalidates its resources.
                try:
                    await consumption.settle()
                except BaseException as cause:
                    if not any(original is cause for original in causes):
                        causes.append(cause)
        cleanup_requests.append(host.retire)
        # Each request and its observation have their own error boundary.
        for request in cleanup_requests:
            try:
                async with asyncio.timeout(15):
                    await observe_cleanup(control=request())
            except BaseException as cause:
                causes.append(cause)
    if len(causes) == 1:
        raise causes[0]
    if causes:
        raise BaseExceptionGroup("Reference matching and cleanup reported failures.", causes)


if __name__ == "__main__":
    asyncio.run(run_demo())
