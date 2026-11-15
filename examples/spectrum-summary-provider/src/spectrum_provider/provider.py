"""Compose the scientific offerings once and assemble each requested workflow."""

from contextlib import asynccontextmanager

from provider_core.contribution import ref
from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import Invocation, OperationPolicy

from .application import PeakWorkflow
from .binding import Baseline, Peaks, create_bindings
from .science import Spectrum


class Provider:
    """Retain this provider's scientific roles within its SDK host lifetime."""

    def __init__(self, *, host: EmbeddedHost) -> None:
        self.host = host
        bindings = create_bindings(host=host)
        self.baseline, = [binding for binding in bindings if isinstance(binding.contribution, Baseline)]
        self.peaks, = [binding for binding in bindings if isinstance(binding.contribution, Peaks)]

    def create_workflow(self, *, spectrum: Spectrum) -> PeakWorkflow:
        """Assemble an unstarted baseline→peaks request using the adopted science.

        Validate and encode input before opening the execution session, so an
        invalid request cannot strand a session that its caller never received.
        The caller owns stopping/closing execution and observing result release.
        """
        baseline_call = self.baseline.call(method=self.baseline.contribution.run)
        payload = baseline_call.input_codec.encode(value=spectrum)
        policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=10,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=4096,
            reject_encoded_result_over_bytes=4096,
        )
        analysis = self.host.open_analysis(allocations=frozenset({self.baseline.allocation, self.peaks.allocation}))
        return PeakWorkflow(
            execution=analysis,
            baseline=Invocation(
                target=ref(call=baseline_call),
                payload=payload,
                states=(),
                reservation=analysis.reservation(allocation=self.baseline.allocation),
                policy=policy,
            ),
            peaks=self.peaks.reference(method=self.peaks.contribution.run),
            peak_reservation=analysis.reservation(allocation=self.peaks.allocation),
            policy=policy,
        )


@asynccontextmanager
async def open_provider(*, result_bytes: int):
    """Own startup and retirement, with a deployment-selected result capacity."""
    # Scientific requirements describe needs; adoption does not reserve hardware.
    async with EmbeddedHost(
        input_bytes=8192,
        result_bytes=result_bytes,
        state_values=1,
        progress_records=1,
        initialization_seconds=5,
        retirement_seconds=5,
    ) as host:
        yield Provider(host=host)
