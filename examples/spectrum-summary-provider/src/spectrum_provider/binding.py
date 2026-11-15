"""Configure independently hosted baseline subtraction and peak detection."""

from __future__ import annotations

from importlib.resources import files
import tomllib

import provider_core.scientific as sdk
from provider_core.embedded_host import EmbeddedHost

from . import science


class Baseline(sdk.Contribution):
    def __init__(self, *, configuration: science.BaselineSettings) -> None:
        super().__init__()
        self.configuration = configuration

    @sdk.operation(concurrency=sdk.ConcurrencyRequirement.PARALLEL)
    def run(self, *, spectrum: science.Spectrum) -> science.Spectrum:
        return science.subtract_baseline(spectrum=spectrum, settings=self.configuration)


class Peaks(sdk.Contribution):
    def __init__(self, *, configuration: science.PeakSettings) -> None:
        super().__init__()
        self.configuration = configuration

    @sdk.operation(concurrency=sdk.ConcurrencyRequirement.PARALLEL)
    def run(self, *, spectrum: science.Spectrum) -> science.PeakReport:
        return science.detect_peaks(spectrum=spectrum, settings=self.configuration)


def create_bindings(
    *, host: EmbeddedHost,
) -> frozenset[sdk.HostedContribution]:
    """Provider-owned assembly; the workflow chooses how these calls compose."""
    package = files(anchor=__package__)
    settings = tomllib.loads(package.joinpath("settings.toml").read_text(encoding="utf-8"))
    requirement = sdk.ResourceRequirement(
        **tomllib.loads(package.joinpath("resources.toml").read_text(encoding="utf-8"))
    )
    return frozenset({
        host.adopt(
            contribution=Baseline(configuration=science.BaselineSettings(**settings["baseline"])),
            requirements=frozenset({requirement}),
        ),
        host.adopt(
            contribution=Peaks(configuration=science.PeakSettings(**settings["peaks"])),
            requirements=frozenset({requirement}),
        ),
    })
