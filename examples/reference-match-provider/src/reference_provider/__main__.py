"""Run two sequential analyses on one local reference-index allocation."""

from __future__ import annotations

import asyncio
import sys
from threading import Event

from provider_core.contribution import CallRef
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.hosting import (
    ExecutionLimits, Invocation, NotAdmitted, Outcome, ResourceGrant, ResultConsumption, ResultState,
    observe_cleanup, observe_completion,
)

from .binding import create_binding
from .science import VectorInput


async def run_demo() -> None:
    """Reuse initialized science; retain results beyond each analysis lifetime."""
    binding = create_binding()
    call, = binding.calls
    host = EmbeddedHost(
        (Allocation("library", binding, (
            ResourceGrant(kind="cpu", amount=1, unit="thread", selector="generic"),
        )),),
        limits=HostLimits(
            analysis_records=2, operation_records=2, input_bytes=8192,
            result_bytes=8192, state_values=1, progress_records=1,
            initialization_seconds=5, retirement_seconds=5,
        ),
    )
    limits = ExecutionLimits(
        queue_seconds=5, execution_seconds=10, cleanup_seconds=5,
        input_bytes=4096, result_bytes=4096,
    )
    revoked = Event()
    cleanup_requests = []
    retained = []
    causes = []

    def require_permission():
        if revoked.is_set():
            raise PermissionError("Reference example dispatch permission was withdrawn.")

    async def print_report(payload: bytes) -> None:
        # Receiver return means flushed local consumption, not durable delivery.
        sys.stdout.buffer.write(payload + b"\n")
        sys.stdout.buffer.flush()

    try:
        for name, values in (("first", (3, 4, 0, 0)), ("second", (0, 0, 1, 1))):
            analysis = host.open_analysis(name, allocation_names=("library",))
            cleanup_requests.append(analysis.close)
            reservation, = analysis.reservations
            operation = analysis.submit(
                Invocation(
                    operation_id="match", target=CallRef(
                        binding.description.binding_id, binding.description.binding_revision, "match",
                    ),
                    payload=call.input_codec.encode(VectorInput(values=values)),
                    states=(), reservation=reservation, limits=limits,
                ),
                dispatch_guard=require_permission,
            )
            if isinstance(operation, NotAdmitted):
                raise RuntimeError(f"Reference match admission refused: {operation.code.value}: {operation.message}")
            async with asyncio.timeout(15):
                completed = await observe_completion(operation)
            if completed.failures:
                causes.extend(failure.cause for failure in completed.failures)
                break
            if completed.outcome is not Outcome.SUCCEEDED or completed.result_state is not ResultState.AVAILABLE:
                raise RuntimeError(f"Reference match produced no usable report: {completed!r}")
            retained.append((operation, completed.result))
            # Same allocation supports sequential analyses. Its index remains
            # initialized; its first result remains retained under a separate owner.
            async with asyncio.timeout(15):
                await observe_cleanup(analysis.close())

    except BaseException as cause:
        causes.append(cause)
    finally:
        revoked.set()
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
                    await observe_cleanup(request())
            except BaseException as cause:
                causes.append(cause)
    if len(causes) == 1:
        raise causes[0]
    if causes:
        raise BaseExceptionGroup("Reference matching and cleanup reported failures.", causes)


if __name__ == "__main__":
    asyncio.run(run_demo())
