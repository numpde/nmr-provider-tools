"""Expose the independently hosted baseline and peak-detection methods."""

from __future__ import annotations

import provider_core.scientific as sdk

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
