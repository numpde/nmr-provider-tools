"""Configure independently hosted baseline subtraction and peak detection."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import tomllib

import provider_core.scientific as sdk
from provider_core.embedded_host import EmbeddedHost, ResourceAllocation

from . import science


class Baseline(sdk.Contribution[science.BaselineSettings]):
    @sdk.operation(concurrency=sdk.ConcurrencyRequirement.PARALLEL)
    def run(self, *, spectrum: science.Spectrum) -> science.Spectrum:
        return science.subtract_baseline(spectrum=spectrum, settings=self.configuration)


class Peaks(sdk.Contribution[science.PeakSettings]):
    @sdk.operation(concurrency=sdk.ConcurrencyRequirement.PARALLEL)
    def run(self, *, spectrum: science.Spectrum) -> science.PeakReport:
        return science.detect_peaks(spectrum=spectrum, settings=self.configuration)


@dataclass(frozen=True, slots=True, kw_only=True)
class SpectrumBindings:
    baseline: sdk.HostedContribution[science.BaselineSettings]
    peaks: sdk.HostedContribution[science.PeakSettings]


def create_bindings(
    *, host: EmbeddedHost, baseline_allocation: ResourceAllocation, peak_allocation: ResourceAllocation,
) -> SpectrumBindings:
    """Provider-owned assembly; the workflow chooses how these calls compose."""
    package = files(anchor=__package__)
    settings = tomllib.loads(package.joinpath("settings.toml").read_text(encoding="utf-8"))
    requirement = sdk.ResourceRequirement(
        **tomllib.loads(package.joinpath("resources.toml").read_text(encoding="utf-8"))
    )
    return SpectrumBindings(
        baseline=host.adopt(
            contribution=Baseline(configuration=science.BaselineSettings(**settings["baseline"])),
            allocation=baseline_allocation,
            resources=(requirement,),
        ),
        peaks=host.adopt(
            contribution=Peaks(configuration=science.PeakSettings(**settings["peaks"])),
            allocation=peak_allocation,
            resources=(requirement,),
        ),
    )
