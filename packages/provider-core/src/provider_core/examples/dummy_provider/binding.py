"""Bind the fake science to the typed contribution interface."""

from __future__ import annotations

import hashlib
import json

from provider_core.contribution import (
    AnalysisHooks,
    AssetDescription,
    CallBinding,
    CallDescription,
    ConcurrencyRequirement,
    ConfigurationDescription,
    DataclassStateBinding,
    ImplementationBinding,
    ImplementationDescription,
    InstanceHooks,
    InterruptionSupport,
    IsolationRequirement,
    JsonDataclassCodec,
    ProgressEvent,
    ResourceRequirement,
    ReturnedState,
    ScientificControl,
    ScientificRejection,
    ScientificReturn,
    StateOutput,
    StateParameter,
    StateUse,
)
from . import science


_VALUES = JsonDataclassCodec(
    format="dummy.values.v1",
    value_type=science.ValuesInput,
)
_EMPTY = JsonDataclassCodec(
    format="dummy.empty.v1",
    value_type=science.EmptyInput,
)
_PREPARATION = JsonDataclassCodec(
    format="dummy.accepted.v1",
    value_type=science.PreparationResult,
)
_PREDICTION = JsonDataclassCodec(
    format="dummy.prediction.v1",
    value_type=science.PredictionResult,
)
_SUM = JsonDataclassCodec(
    format="dummy.sum.v1",
    value_type=science.SumResult,
)
_NO_STATE = DataclassStateBinding(value_type=science.NoState)
_CALCULATION_STATE = DataclassStateBinding(value_type=science.CalculationState)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _prepare(
    model: science.Model,
    analysis: science.AnalysisState,
    candidate: science.ValuesInput,
    state: science.NoState,
    control: ScientificControl,
) -> ScientificReturn[science.PreparationResult]:
    try:
        prepared = science.prepare(analysis, candidate)
    except science.InputRejected as error:
        raise ScientificRejection(error.code, str(error)) from error
    return ScientificReturn(
        value=science.PreparationResult(count=len(prepared.values)),
        states=(
            ReturnedState(
                name="prepared",
                kind="dummy.prepared.v1",
                value=prepared,
                dispose=science.dispose_prepared,
            ),
        ),
        dispose_value=None,
    )


def _calculate(
    model: science.Model,
    analysis: science.AnalysisState,
    request: science.EmptyInput,
    state: science.CalculationState,
    control: ScientificControl,
) -> ScientificReturn[science.PredictionResult]:
    control.report_progress(ProgressEvent("calculation", 0, 1))
    result = science.calculate(analysis, request, state)
    control.report_progress(ProgressEvent("calculation", 1, 1))
    return ScientificReturn(
        value=result,
        states=(),
        dispose_value=None,
    )


def _sum(
    instance: None,
    analysis: None,
    candidate: science.ValuesInput,
    state: science.NoState,
    control: ScientificControl,
) -> ScientificReturn[science.SumResult]:
    return ScientificReturn(
        value=science.sum_values(candidate),
        states=(),
        dispose_value=None,
    )


def _cpu_requirement() -> ResourceRequirement:
    return ResourceRequirement(
        kind="cpu",
        amount=1,
        unit="thread",
        selector="generic",
        exclusive=False,
    )


def weighted_binding(
    settings: science.Settings,
    coefficients: tuple[float, ...],
) -> ImplementationBinding[science.Model, science.AnalysisState]:
    """Bind immutable settings and coefficient assets to the weighted model."""
    science.require_configuration(settings, coefficients)

    configuration = ConfigurationDescription(
        format="dummy.settings.v1",
        digest=_digest({"scale": settings.scale, "offset": settings.offset}),
    )
    coefficient_document = {
        "coefficients": tuple(float(value) for value in coefficients),
    }
    coefficient_bytes = _canonical_json(coefficient_document)
    coefficient_asset = AssetDescription(
        name="coefficients",
        media_type="application/vnd.nmr-provider-tools.dummy-coefficients+json",
        digest="sha256:" + hashlib.sha256(coefficient_bytes).hexdigest(),
        size_bytes=len(coefficient_bytes),
    )
    revision = _digest({
        "implementation": "dummy.weighted.v2",
        "configuration": configuration.digest,
        "assets": (coefficient_asset.digest,),
    })

    prepare = CallDescription(
        name="prepare",
        input_format=_VALUES.format,
        result_format=_PREPARATION.format,
        state_inputs=(),
        state_outputs=(StateOutput("prepared", "dummy.prepared.v1"),),
        resources=(_cpu_requirement(),),
        concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=InterruptionSupport.NONE,
        progress=False,
    )
    calculate = CallDescription(
        name="calculate",
        input_format=_EMPTY.format,
        result_format=_PREDICTION.format,
        state_inputs=(
            StateParameter(
                name="prepared",
                kind="dummy.prepared.v1",
                use=StateUse.CONSUME,
            ),
        ),
        state_outputs=(),
        resources=(_cpu_requirement(),),
        concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=InterruptionSupport.NONE,
        progress=True,
    )
    description = ImplementationDescription(
        binding_id="dummy.weighted",
        binding_revision=revision,
        contract_version=1,
        configuration=configuration,
        assets=(coefficient_asset,),
        calls=(prepare, calculate),
        resident_resources=(
            ResourceRequirement(
                kind="host-memory",
                amount=max(1, 8 * len(coefficients)),
                unit="byte",
                selector="python",
                exclusive=False,
            ),
        ),
        isolation=IsolationRequirement.SHARED_PROCESS,
    )
    return ImplementationBinding(
        description=description,
        instance=InstanceHooks(
            construct=lambda: science.create_model(settings, coefficients),
            initialize=science.load_model,
            dispose=science.dispose_model,
        ),
        analysis=AnalysisHooks(
            construct=science.create_analysis,
            initialize=science.initialize_analysis,
            dispose=science.dispose_analysis,
        ),
        calls=(
            CallBinding(
                description=prepare,
                input_codec=_VALUES,
                state_binding=_NO_STATE,
                scientific_call=_prepare,
                result_codec=_PREPARATION,
            ),
            CallBinding(
                description=calculate,
                input_codec=_EMPTY,
                state_binding=_CALCULATION_STATE,
                scientific_call=_calculate,
                result_codec=_PREDICTION,
            ),
        ),
    )


def sum_binding() -> ImplementationBinding[None, None]:
    """A stateless implementation uses the same typed call interface."""
    configuration = ConfigurationDescription(
        format="dummy.none.v1",
        digest=_digest({}),
    )
    run = CallDescription(
        name="run",
        input_format=_VALUES.format,
        result_format=_SUM.format,
        state_inputs=(),
        state_outputs=(),
        resources=(_cpu_requirement(),),
        concurrency=ConcurrencyRequirement.PARALLEL,
        interruption=InterruptionSupport.NONE,
        progress=False,
    )
    return ImplementationBinding(
        description=ImplementationDescription(
            binding_id="dummy.sum",
            binding_revision=_digest({"implementation": "dummy.sum.v2"}),
            contract_version=1,
            configuration=configuration,
            assets=(),
            calls=(run,),
            resident_resources=(),
            isolation=IsolationRequirement.SHARED_PROCESS,
        ),
        instance=None,
        analysis=None,
        calls=(
            CallBinding(
                description=run,
                input_codec=_VALUES,
                state_binding=_NO_STATE,
                scientific_call=_sum,
                result_codec=_SUM,
            ),
        ),
    )
