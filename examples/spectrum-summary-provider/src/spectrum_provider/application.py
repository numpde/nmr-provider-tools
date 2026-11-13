"""Own one baseline-to-peaks sequence through the public execution interface.

This module knows neither scientific objects nor a backend's implementation.
One controller calls its methods serially; cancelling a waiter leaves the
original operation and its retained result owned by this workflow.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from provider_core.contribution import CallRef
from provider_core.hosting import (
    AnalysisExecution,
    ExecutionLimits,
    Invocation,
    NotAdmitted,
    Observation,
    Operation,
    Outcome,
    ReservationRef,
    ResultRelease,
    ResultState,
    observe_cleanup,
    observe_completion,
)


class PeakWorkflow:
    """Retain the two operations and transfer portable bytes between them.

    The application chooses this fixed scientific sequence. The execution
    backend owns dispatch, permission enforcement and result retention. No
    scientific state handle crosses the two independent bindings.
    """

    def __init__(
        self,
        *,
        execution: AnalysisExecution,
        baseline: Invocation,
        peaks: CallRef,
        peak_reservation: ReservationRef,
        limits: ExecutionLimits,
        dispatch_guard: Callable[[], None],
    ) -> None:
        self.execution = execution
        self.baseline_invocation = baseline
        self.peak_target = peaks
        self.peak_reservation = peak_reservation
        self.limits = limits
        self.dispatch_guard = dispatch_guard
        self.baseline_operation: Operation | None = None
        self.peak_operation: Operation | None = None
        self.baseline_release: ResultRelease | None = None
        self.peak_release: ResultRelease | None = None

    def start(self) -> Operation | NotAdmitted:
        """Acquire the operation handle before any cancellable wait."""
        if self.baseline_operation is not None:
            return self.baseline_operation
        accepted = self.execution.submit(
            self.baseline_invocation, dispatch_guard=self.dispatch_guard,
        )
        if not isinstance(accepted, NotAdmitted):
            self.baseline_operation = accepted
        return accepted

    async def finish(self) -> Observation | NotAdmitted:
        """Resume observation or downstream admission without repeating science."""
        if self.baseline_operation is None:
            raise RuntimeError("Start the baseline operation before waiting for peaks.")
        if self.peak_operation is None:
            baseline = await observe_completion(self.baseline_operation)
            # Science may succeed while encoding fails. Keep both facts intact.
            if baseline.outcome is not Outcome.SUCCEEDED or baseline.result_state is not ResultState.AVAILABLE:
                return baseline
            payload = await self.baseline_operation.read_result(baseline.result)
            accepted = self.execution.submit(
                Invocation(
                    operation_id="detect-peaks",
                    target=self.peak_target,
                    payload=payload,
                    states=(),
                    reservation=self.peak_reservation,
                    limits=self.limits,
                ),
                dispatch_guard=self.dispatch_guard,
            )
            if isinstance(accepted, NotAdmitted):
                # Admission failed before effects; the intermediate copy stays owned.
                return accepted
            self.peak_operation = accepted
            # The accepted invocation now owns its input bytes. This is an
            # in-memory handoff, not durable report acceptance or publication.
            self.baseline_release = self.baseline_operation.release_result(baseline.result)
        # Intermediate cleanup is an independent owner. A release failure must
        # not hide an accepted peak operation or its usable result. The caller
        # observes baseline_release separately when settling the workflow.
        return await observe_completion(self.peak_operation)

    async def deliver(
        self, accept: Callable[[bytes], Awaitable[None]],
    ) -> Observation | NotAdmitted:
        """Release the final copy only after the caller's consumer accepts it.

        Consumer failure leaves the result readable. A consumer that can accept
        and then lose its acknowledgement must make repeated acceptance safe.
        """
        completed = await self.finish()
        if isinstance(completed, NotAdmitted):
            return completed
        if self.peak_operation is None:
            return completed
        if self.peak_release is None:
            if completed.outcome is not Outcome.SUCCEEDED or completed.result_state is not ResultState.AVAILABLE:
                return completed
            payload = await self.peak_operation.read_result(completed.result)
            await accept(payload)
            self.peak_release = self.peak_operation.release_result(completed.result)
        await observe_cleanup(self.peak_release)
        return await self.peak_operation.observe(after=None, wait_seconds=0)
