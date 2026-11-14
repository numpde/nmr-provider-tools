"""Configure independently hosted baseline subtraction and peak detection."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import tomllib

import provider_core.scientific as sdk

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
    baseline: sdk.Registration[science.BaselineSettings]
    peaks: sdk.Registration[science.PeakSettings]


def create_bindings() -> SpectrumBindings:
    """Provider-owned assembly; the workflow chooses how these calls compose."""
    package = files(anchor=__package__)
    settings = tomllib.loads(package.joinpath("settings.toml").read_text(encoding="utf-8"))
    requirement = sdk.ResourceRequirement(
        **tomllib.loads(package.joinpath("resources.toml").read_text(encoding="utf-8"))
    )
    return SpectrumBindings(
        baseline=sdk.Registration(
            contribution=Baseline, configuration=science.BaselineSettings(**settings["baseline"]),
            resources=(requirement,),
        ),
        peaks=sdk.Registration(
            contribution=Peaks, configuration=science.PeakSettings(**settings["peaks"]),
            resources=(requirement,),
        ),
    )
