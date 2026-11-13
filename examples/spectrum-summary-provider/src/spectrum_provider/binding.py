"""Bind two fixed scientific implementations to packaged settings and resources."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import tomllib

import provider_core.contribution as contribution
import provider_core.contribution_declaration as declaration

from . import science


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineBinding(contribution.ImplementationBinding[None, None]):
    run: contribution.CallBinding[
        science.Spectrum, contribution.NoState, science.Spectrum,
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class PeakBinding(contribution.ImplementationBinding[None, None]):
    run: contribution.CallBinding[
        science.Spectrum, contribution.NoState, science.PeakReport,
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class SpectrumBindings:
    baseline: BaselineBinding
    peaks: PeakBinding


def create_bindings() -> SpectrumBindings:
    """Capture immutable deployment settings in independent stateless bindings.

    This provider-owned assembly helper is invoked explicitly by startup code
    or tests. Its name and signature are local conventions, not SDK entrypoints.
    The caller passes the returned ImplementationBinding objects to the host.
    """
    spectrum_format = "spectrum.samples.v1"
    package = files(__package__)
    settings = tomllib.loads(package.joinpath("settings.toml").read_text(encoding="utf-8"))
    baseline_settings = science.BaselineSettings(**settings["baseline"])
    peak_settings = science.PeakSettings(**settings["peaks"])
    requirement = contribution.ResourceRequirement(
        **tomllib.loads(package.joinpath("resources.toml").read_text(encoding="utf-8"))
    )
    resource_asset = declaration.json_asset(
        requirement,
        name="resources",
        format_id="spectrum.resources.v1",
        media_type="application/vnd.nmr-provider-tools.spectrum-resources+json",
    )

    def baseline(scientific_input: science.Spectrum) -> science.Spectrum:
        return science.subtract_baseline(scientific_input, baseline_settings)

    def peaks(scientific_input: science.Spectrum) -> science.PeakReport:
        return science.detect_peaks(scientific_input, peak_settings)

    baseline_call = contribution.CallBinding.from_function(
        name="run",
        input_type=science.Spectrum,
        result_type=science.Spectrum,
        input_format=spectrum_format,
        result_format=spectrum_format,
        resources=(requirement,),
        concurrency=contribution.ConcurrencyRequirement.PARALLEL,
        function=baseline,
    )
    peak_call = contribution.CallBinding.from_function(
        name="run",
        input_type=science.Spectrum,
        result_type=science.PeakReport,
        input_format=spectrum_format,
        result_format="spectrum.peaks.v1",
        resources=(requirement,),
        concurrency=contribution.ConcurrencyRequirement.PARALLEL,
        function=peaks,
    )
    baseline_configuration = declaration.json_configuration(
        baseline_settings,
        format_id="spectrum.baseline-settings.v1",
    )
    peak_configuration = declaration.json_configuration(
        peak_settings,
        format_id="spectrum.peak-settings.v1",
    )
    assets = (resource_asset,)

    baseline_binding = BaselineBinding(
        identity=contribution.ConfiguredIdentity(
            binding_id="spectrum.baseline",
            implementation="spectrum.baseline.v1",
            contract_version=1,
            configuration=baseline_configuration,
            assets=assets,
        ),
        run=baseline_call,
        resident_resources=(),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=None,
        analysis=None,
    )
    peak_binding = PeakBinding(
        identity=contribution.ConfiguredIdentity(
            binding_id="spectrum.peaks",
            implementation="spectrum.peaks.v1",
            contract_version=1,
            configuration=peak_configuration,
            assets=assets,
        ),
        run=peak_call,
        resident_resources=(),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=None,
        analysis=None,
    )
    return SpectrumBindings(baseline=baseline_binding, peaks=peak_binding)
