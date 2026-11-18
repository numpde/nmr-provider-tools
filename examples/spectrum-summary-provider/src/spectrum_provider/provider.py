"""Compose the scientific offerings once and assemble each requested workflow."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import pathlib
import tomllib

from provider_core.contribution import ref
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import Invocation, OperationPolicy
from provider_core.scientific import HostedContribution, ResourceRequirement

from .application import PeakWorkflow
from .binding import BaselineCorrector, PeakDetector
from . import science
from .science import Spectrum


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = pathlib.Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """Retain the exact scientific roles and operation policy selected at startup."""

    execution_host: InProcessExecutionHost
    baseline_corrector: HostedContribution
    peak_detector: HostedContribution
    operation_policy: OperationPolicy

    def create_workflow(self, *, spectrum: Spectrum) -> PeakWorkflow:
        """Assemble an unstarted baseline→peaks request using the adopted science.

        Validate and encode input before opening the execution session, so an
        invalid request cannot strand a session that its caller never received.
        The caller owns stopping/closing execution and observing result release.
        """
        baseline_call_binding = self.baseline_corrector.call(method=self.baseline_corrector.contribution.run)
        spectrum_payload = baseline_call_binding.input_codec.encode(value=spectrum)

        analysis_executor = self.execution_host.open_analysis(
            allocations=frozenset(
                {
                    self.baseline_corrector.allocation,
                    self.peak_detector.allocation,
                }
            ),
        )

        return PeakWorkflow(
            analysis_executor=analysis_executor,
            baseline_invocation=Invocation(
                target=ref(call=baseline_call_binding),
                payload=spectrum_payload,
                states={},
                reservation=analysis_executor.reservation(allocation=self.baseline_corrector.allocation),
                policy=self.operation_policy,
            ),
            peak_detection_call_ref=self.peak_detector.reference(method=self.peak_detector.contribution.run),
            peak_detection_reservation_ref=analysis_executor.reservation(allocation=self.peak_detector.allocation),
            operation_policy=self.operation_policy,
        )


@asynccontextmanager
async def open_provider() -> AsyncIterator[Provider]:
    """Read deployment choices once and own the adopted science until retirement."""

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)

    operation_policy = OperationPolicy(**provider_config["execution"]["operation_policy"])

    resource_requirements = frozenset(
        ResourceRequirement(**resource_requirement)
        for resource_requirement in provider_config["execution"]["resource_requirement"]
    )

    async with InProcessExecutionHost(**provider_config["execution"]["in_process_execution_host"]) as execution_host:
        # Baseline and peak detection are mandatory roles in this scientific
        # sequence. Keep their exact handles rather than rediscovering a role.
        yield Provider(
            execution_host=execution_host,
            baseline_corrector=execution_host.adopt(
                contribution=BaselineCorrector(
                    configuration=science.BaselineSettings(**provider_config["science"]["baseline"]["settings"]),
                ),
                requirements=resource_requirements,
            ),
            peak_detector=execution_host.adopt(
                contribution=PeakDetector(
                    configuration=science.PeakSettings(**provider_config["science"]["peaks"]["settings"]),
                ),
                requirements=resource_requirements,
            ),
            operation_policy=operation_policy,
        )
