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
        analysis_executor: AnalysisExecution,
        baseline_invocation: Invocation,
        peak_detection_call_ref: CallRef,
        peak_detection_reservation_ref: ReservationRef,
        operation_policy: OperationPolicy,
    ) -> None:
        self.analysis_executor = analysis_executor
        self.baseline_submission = Submission(analysis_executor=analysis_executor, invocation=baseline_invocation)
        self.peak_detection_call_ref = peak_detection_call_ref
        self.peak_detection_reservation_ref = peak_detection_reservation_ref
        self.operation_policy = operation_policy
        self.peak_detection_submission: Submission | None = None
        self.peak_detection_refusal: NotAdmitted | None = None
        self.baseline_report_release: ResultRelease | None = None

    def start(self) -> Operation | NotAdmitted:
        """Explicitly deliver the retained baseline request before waiting."""

        return self.baseline_submission.submit()

    async def finish(self) -> Observation | NotAdmitted:
        """Resume observation or downstream admission without repeating science."""

        if self.baseline_submission.operation is None:
            raise RuntimeError("Start the baseline operation before waiting for peaks.")

        if self.peak_detection_refusal is not None:
            return self.peak_detection_refusal

        if self.peak_detection_submission is None or self.peak_detection_submission.operation is None:
            baseline_observation = await self.baseline_submission.wait()

            # Science may succeed while encoding fails. Keep both facts intact.
            if baseline_observation.outcome is not Outcome.SUCCEEDED or baseline_observation.result_state is not ResultState.AVAILABLE:
                return baseline_observation

            corrected_spectrum_payload = await self.baseline_submission.operation.read_result(result=baseline_observation.result)

            if self.peak_detection_submission is None:
                self.peak_detection_submission = Submission(
                    analysis_executor=self.analysis_executor,
                    invocation=Invocation(
                        target=self.peak_detection_call_ref,
                        payload=corrected_spectrum_payload,
                        states=(),
                        reservation=self.peak_detection_reservation_ref,
                        policy=self.operation_policy,
                    ),
                )

            peak_detection_admission = self.peak_detection_submission.submit()

            if isinstance(peak_detection_admission, NotAdmitted):
                if peak_detection_admission.code is AdmissionCode.CLOSED:
                    # Stopping execution makes downstream admission impossible.
                    # This workflow no longer needs the intermediate result.
                    self.baseline_report_release = self.baseline_submission.operation.release_result(result=baseline_observation.result)
                    self.peak_detection_refusal = peak_detection_admission

                # A capacity refusal can still be retried using the retained input.
                return peak_detection_admission

            # The accepted invocation now owns its input bytes. This is an
            # in-memory handoff, not durable report acceptance or publication.
            self.baseline_report_release = self.baseline_submission.operation.release_result(result=baseline_observation.result)

        # Intermediate cleanup is an independent owner. A release failure must
        # not hide an accepted peak operation or its usable result. The caller
        # observes baseline_report_release separately when settling the workflow.
        return await self.peak_detection_submission.wait()
