"""Own one baseline-to-peaks sequence through the public execution interface.

This module knows neither scientific objects nor a backend's implementation.
One controller calls its methods serially; cancelling a waiter leaves the
original operation and its retained result owned by this workflow.
"""

from __future__ import annotations


from provider_core.contribution import CallRef
from provider_core.hosting import (
    AnalysisExecution,
    AdmissionCode,
    OperationPolicy,
    Invocation,
    NotAdmitted,
    Observation,
    Operation,
    Outcome,
    ReservationRef,
    ResultRelease,
    ResultState,
)

from provider_core.submission import Submission


class PeakWorkflow:
    """Retain the two operations and transfer portable bytes between them.

    The application chooses this fixed scientific sequence. The execution
    backend owns dispatch, cancellation enforcement and result retention. No
    scientific state handle crosses the two independent bindings.
    """

    def __init__(
        self,
        *,
        execution: AnalysisExecution,
        baseline: Invocation,
        peaks: CallRef,
        peak_reservation: ReservationRef,
        policy: OperationPolicy,
    ) -> None:
        self.execution = execution
        self.baseline = Submission(execution=execution, invocation=baseline)
        self.peak_target = peaks
        self.peak_reservation = peak_reservation
        self.policy = policy
        self.peaks: Submission | None = None
        self.peak_refusal: NotAdmitted | None = None
        self.baseline_release: ResultRelease | None = None

    def start(self) -> Operation | NotAdmitted:
        """Explicitly deliver the retained baseline request before waiting."""
        return self.baseline.submit()

    async def finish(self) -> Observation | NotAdmitted:
        """Resume observation or downstream admission without repeating science."""
        if self.baseline.operation is None:
            raise RuntimeError("Start the baseline operation before waiting for peaks.")
        if self.peak_refusal is not None:
            return self.peak_refusal
        if self.peaks is None or self.peaks.operation is None:
            baseline = await self.baseline.wait()
            # Science may succeed while encoding fails. Keep both facts intact.
            if baseline.outcome is not Outcome.SUCCEEDED or baseline.result_state is not ResultState.AVAILABLE:
                return baseline
            payload = await self.baseline.operation.read_result(result=baseline.result)
            if self.peaks is None:
                self.peaks = Submission(execution=self.execution, invocation=Invocation(
                    target=self.peak_target,
                    payload=payload,
                    states=(),
                    reservation=self.peak_reservation,
                    policy=self.policy,
                ))
            accepted = self.peaks.submit()
            if isinstance(accepted, NotAdmitted):
                if accepted.code is AdmissionCode.CLOSED:
                    # Stopping execution makes downstream admission impossible.
                    # This workflow no longer needs the intermediate result.
                    self.baseline_release = self.baseline.operation.release_result(result=baseline.result)
                    self.peak_refusal = accepted
                # A capacity refusal can still be retried using the retained input.
                return accepted
            # The accepted invocation now owns its input bytes. This is an
            # in-memory handoff, not durable report acceptance or publication.
            self.baseline_release = self.baseline.operation.release_result(result=baseline.result)
        # Intermediate cleanup is an independent owner. A release failure must
        # not hide an accepted peak operation or its usable result. The caller
        # observes baseline_release separately when settling the workflow.
        return await self.peaks.wait()
