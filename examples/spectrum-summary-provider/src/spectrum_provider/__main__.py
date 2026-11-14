"""Compose and run the local example; no Jobs service or API is implied."""

from __future__ import annotations

import asyncio
import sys
from threading import Event

from provider_core.contribution import ref
from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import (
    OperationPolicy, Invocation, NotAdmitted, ResultConsumption,
    ResultState, observe_cleanup,
)

from .application import PeakWorkflow
from .binding import Baseline, Peaks, create_bindings
from .science import Spectrum


async def run_demo() -> None:
    """Print one consumed report and observe analysis and host cleanup separately."""
    # Scientific requirements come from resources.toml; adoption records them
    # without claiming that hardware has been reserved.
    host = EmbeddedHost(
        input_bytes=8192,
        result_bytes=8192,
        state_values=1,
        progress_records=1,
        initialization_seconds=5,
        retirement_seconds=5,
    )
    analysis = None
    workflow = None
    consumption = None
    revoked = Event()
    causes = []

    def require_permission() -> None:
        if revoked.is_set():
            raise PermissionError("The local example withdrew dispatch permission.")

    async def print_report(payload: bytes) -> None:
        # Flushed stdout is this demo's consumer, not a durable report queue.
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()

    try:
        bindings = create_bindings(
            host=host,
        )
        baseline, peaks = bindings.baseline, bindings.peaks
        analysis = host.open_analysis(allocations=(baseline.allocation, peaks.allocation))
        baseline_reservation = analysis.reservation(allocation=baseline.allocation)
        peak_reservation = analysis.reservation(allocation=peaks.allocation)
        baseline_call = baseline.call(method=Baseline.run)
        operation_policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=10,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=4096,
            reject_encoded_result_over_bytes=4096,
        )
        workflow = PeakWorkflow(
            execution=analysis,
            baseline=Invocation(
                target=ref(call=baseline_call),
                payload=baseline_call.input_codec.encode(
                    value=Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)),
                ),
                states=(), reservation=baseline_reservation, policy=operation_policy,
            ),
            peaks=peaks.reference(method=Peaks.run),
            peak_reservation=peak_reservation, policy=operation_policy,
            dispatch_guard=require_permission,
        )
        started = workflow.start()
        if isinstance(started, NotAdmitted):
            raise RuntimeError(f"Baseline admission refused: {started.code.value}: {started.message}")
        async with asyncio.timeout(15):
            result = await workflow.finish()
        if isinstance(result, NotAdmitted):
            raise RuntimeError(f"Peak admission refused: {result.code.value}: {result.message}")
        if result.failures:
            causes.extend(failure.cause for failure in result.failures)
        elif result.result_state is ResultState.AVAILABLE:
            consumption = ResultConsumption(
                operation=workflow.peak_operation, result=result.result, receive=print_report,
            )
            async with asyncio.timeout(15):
                await consumption.consume()
        else:
            raise RuntimeError(f"Peak report was not delivered: {result!r}")
    except BaseException as cause:
        causes.append(cause)
    finally:
        revoked.set()
        if consumption is not None and consumption.acceptance is not None:
            try:
                # Complete the original acceptance and release observation,
                # deferring further cancellation until their outcome is known.
                await consumption.settle()
            except BaseException as cause:
                if not any(original is cause for original in causes):
                    causes.append(cause)
        if workflow is not None and workflow.baseline_release is not None:
            try:
                async with asyncio.timeout(15):
                    await observe_cleanup(control=workflow.baseline_release)
            except BaseException as cause:
                causes.append(cause)
        # Try both owners even if one reports failure, preserving every cause.
        cleanup_requests = []
        if analysis is not None:
            cleanup_requests.append(analysis.close)
        cleanup_requests.append(host.retire)
        for request in cleanup_requests:
            try:
                control = request()
                async with asyncio.timeout(15):
                    await observe_cleanup(control=control)
            except BaseException as cause:
                causes.append(cause)
    if len(causes) == 1:
        raise causes[0]
    if causes:
        raise BaseExceptionGroup("The example and its cleanup reported failures.", causes)


if __name__ == "__main__":
    asyncio.run(run_demo())
