"""Compose the scientific offerings once and assemble each requested workflow."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import tomllib

from provider_core.contribution import ref
from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import Invocation, OperationPolicy
from provider_core.scientific import HostedContribution, ResourceRequirement

from .application import PeakWorkflow
from .binding import Baseline, Peaks
from . import science
from .science import Spectrum


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """Retain the exact scientific roles and operation policy selected at startup."""

    host: EmbeddedHost
    baseline: HostedContribution
    peaks: HostedContribution
    operation_policy: OperationPolicy

    def create_workflow(self, *, spectrum: Spectrum) -> PeakWorkflow:
        """Assemble an unstarted baseline→peaks request using the adopted science.

        Validate and encode input before opening the execution session, so an
        invalid request cannot strand a session that its caller never received.
        The caller owns stopping/closing execution and observing result release.
        """
        baseline_call = self.baseline.call(method=self.baseline.contribution.run)
        payload = baseline_call.input_codec.encode(value=spectrum)
        analysis = self.host.open_analysis(allocations=frozenset({self.baseline.allocation, self.peaks.allocation}))
        return PeakWorkflow(
            execution=analysis,
            baseline=Invocation(
                target=ref(call=baseline_call),
                payload=payload,
                states=(),
                reservation=analysis.reservation(allocation=self.baseline.allocation),
                policy=self.operation_policy,
            ),
            peaks=self.peaks.reference(method=self.peaks.contribution.run),
            peak_reservation=analysis.reservation(allocation=self.peaks.allocation),
            policy=self.operation_policy,
        )


@asynccontextmanager
async def open_provider() -> AsyncIterator[Provider]:
    """Read deployment choices once and own the adopted science until retirement."""
    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        configuration = tomllib.load(source)
    policy = OperationPolicy(**configuration["execution"]["operation_policy"])
    requirements = frozenset(
        ResourceRequirement(**requirement)
        for requirement in configuration["execution"]["resource_requirement"]
    )
    async with EmbeddedHost(**configuration["execution"]["embedded_host"]) as host:
        # Baseline and peak detection are mandatory roles in this scientific
        # sequence. Keep their exact handles rather than rediscovering a role.
        yield Provider(
            host=host,
            baseline=host.adopt(
                contribution=Baseline(configuration=science.BaselineSettings(**configuration["science"]["baseline"]["settings"])),
                requirements=requirements,
            ),
            peaks=host.adopt(
                contribution=Peaks(configuration=science.PeakSettings(**configuration["science"]["peaks"]["settings"])),
                requirements=requirements,
            ),
            operation_policy=policy,
        )
