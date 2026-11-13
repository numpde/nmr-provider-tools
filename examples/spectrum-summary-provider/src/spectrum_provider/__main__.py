"""Compose and run the local example; no Jobs service or API is implied."""

from __future__ import annotations

import asyncio
import sys
from threading import Event

from provider_core.contribution import ref
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.hosting import (
    ExecutionLimits, Invocation, NotAdmitted, ResourceGrant, ResultConsumption,
    ResultState, observe_cleanup,
)

from .application import PeakWorkflow
from .binding import create_bindings
from .science import Spectrum


async def run_demo() -> None:
    """Print one consumed report and observe analysis and host cleanup separately."""
    bindings = create_bindings()
    baseline, peaks = bindings.baseline, bindings.peaks
    # This local deployment grants one generic CPU thread to each allocation.
    # Scientific resource requirements remain in resources.toml; they grant no access.
    grants = (ResourceGrant(kind="cpu", amount=1, unit="thread", selector="generic"),)
    host = EmbeddedHost(
        (Allocation("baseline", baseline, grants), Allocation("peaks", peaks, grants)),
        limits=HostLimits(
            analysis_records=1, operation_records=2, input_bytes=8192,
            result_bytes=8192, state_values=1, progress_records=1,
            initialization_seconds=5, retirement_seconds=5,
        ),
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
        analysis = host.open_analysis("spectrum-demo", allocation_names=("baseline", "peaks"))
        reservations = {item.allocation: item for item in analysis.reservations}
        baseline_call = baseline.run
        limits = ExecutionLimits(
            queue_seconds=5, execution_seconds=10, cleanup_seconds=5,
            input_bytes=4096, result_bytes=4096,
        )
        workflow = PeakWorkflow(
            execution=analysis,
            baseline=Invocation(
                operation_id="subtract-baseline",
                target=ref(baseline.run),
                payload=baseline_call.input_codec.encode(
                    Spectrum(positions=(0, 1, 2, 3, 4), intensities=(1, 4, 2, 5, 1)),
                ),
                states=(), reservation=reservations["baseline"], limits=limits,
            ),
            peaks=ref(peaks.run),
            peak_reservation=reservations["peaks"], limits=limits,
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
                    await observe_cleanup(workflow.baseline_release)
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
                    await observe_cleanup(control)
            except BaseException as cause:
                causes.append(cause)
    if len(causes) == 1:
        raise causes[0]
    if causes:
        raise BaseExceptionGroup("The example and its cleanup reported failures.", causes)


if __name__ == "__main__":
    asyncio.run(run_demo())
