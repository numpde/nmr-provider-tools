"""Bind two fixed scientific implementations to packaged settings and resources."""

from __future__ import annotations

from importlib.resources import files
import tomllib

import provider_core.contribution as contribution
import provider_core.contribution_declaration as declaration

from . import science


def create_bindings() -> tuple[
    contribution.ImplementationBinding[None, None],
    contribution.ImplementationBinding[None, None],
]:
    """Capture immutable deployment settings in independent stateless bindings."""
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

    def baseline(
        *,
        scientific_input: science.Spectrum,
    ) -> contribution.ScientificReturn[science.Spectrum]:
        return contribution.ScientificReturn(
            value=science.subtract_baseline(scientific_input, baseline_settings),
        )

    def peaks(
        *,
        scientific_input: science.Spectrum,
    ) -> contribution.ScientificReturn[science.PeakReport]:
        return contribution.ScientificReturn(
            value=science.detect_peaks(scientific_input, peak_settings),
        )

    baseline_call = contribution.CallBinding.from_json_dataclasses(
        name="run",
        input_format=spectrum_format,
        result_format=spectrum_format,
        state_inputs=(),
        state_outputs=(),
        resources=(requirement,),
        concurrency=contribution.ConcurrencyRequirement.PARALLEL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=False,
        scientific_call=baseline,
    )
    peak_call = contribution.CallBinding.from_json_dataclasses(
        name="run",
        input_format=spectrum_format,
        result_format="spectrum.peaks.v1",
        state_inputs=(),
        state_outputs=(),
        resources=(requirement,),
        concurrency=contribution.ConcurrencyRequirement.PARALLEL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=False,
        scientific_call=peaks,
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

    baseline_binding = contribution.ImplementationBinding.from_calls(
        binding_id="spectrum.baseline",
        binding_revision=declaration.binding_revision(
            implementation="spectrum.baseline.v1",
            configuration=baseline_configuration,
            assets=assets,
        ),
        contract_version=1,
        configuration=baseline_configuration,
        assets=assets,
        calls=(baseline_call,),
        resident_resources=(),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=None,
        analysis=None,
    )
    peak_binding = contribution.ImplementationBinding.from_calls(
        binding_id="spectrum.peaks",
        binding_revision=declaration.binding_revision(
            implementation="spectrum.peaks.v1",
            configuration=peak_configuration,
            assets=assets,
        ),
        contract_version=1,
        configuration=peak_configuration,
        assets=assets,
        calls=(peak_call,),
        resident_resources=(),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=None,
        analysis=None,
    )
    return (baseline_binding, peak_binding)
