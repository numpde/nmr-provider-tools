"""Bind the dummy science to the shared contribution interface."""

from __future__ import annotations

import hashlib
from functools import partial
from typing import Final, NoReturn

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


_PREPARED_VALUES_KIND: Final = "dummy.prepared-values.v1"

_SETTINGS_CODEC = JsonDataclassCodec(
    format="dummy.settings.v1",
    value_type=science.WeightedSumSettings,
)
_SUM_CONFIGURATION_CODEC = JsonDataclassCodec(
    format="dummy.sum-configuration.v1",
    value_type=science.PlainSumConfiguration,
)
_COEFFICIENTS_CODEC = JsonDataclassCodec(
    format="dummy.coefficients.v1",
    value_type=science.CoefficientSet,
)
_PREPARATION_INPUT_CODEC = JsonDataclassCodec(
    format="dummy.preparation-input.v1",
    value_type=science.PreparationInput,
)
_CALCULATION_INPUT_CODEC = JsonDataclassCodec(
    format="dummy.calculation-input.v1",
    value_type=science.CalculationInput,
)
_SUM_INPUT_CODEC = JsonDataclassCodec(
    format="dummy.sum-input.v1",
    value_type=science.SumInput,
)
_PREPARATION_RESULT_CODEC = JsonDataclassCodec(
    format="dummy.preparation-result.v1",
    value_type=science.PreparationResult,
)
_PREDICTION_RESULT_CODEC = JsonDataclassCodec(
    format="dummy.prediction-result.v1",
    value_type=science.PredictionResult,
)
_SUM_RESULT_CODEC = JsonDataclassCodec(
    format="dummy.sum-result.v1",
    value_type=science.SumResult,
)

_NO_LIVE_STATE_BINDING = DataclassStateBinding(
    value_type=science.NoLiveState,
)
_CALCULATION_STATE_BINDING = DataclassStateBinding(
    value_type=science.CalculationState,
)

_CPU_THREAD_REQUIREMENT = ResourceRequirement(
    kind="cpu",
    amount=1,
    unit="thread",
    selector="generic",
    exclusive=False,
)

_PREPARE_CALL = CallDescription(
    name="prepare",
    input_format=_PREPARATION_INPUT_CODEC.format,
    result_format=_PREPARATION_RESULT_CODEC.format,
    state_inputs=(),
    state_outputs=(
        StateOutput(
            name="prepared",
            kind=_PREPARED_VALUES_KIND,
        ),
    ),
    resources=(_CPU_THREAD_REQUIREMENT,),
    concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
    interruption=InterruptionSupport.NONE,
    progress=False,
)
_CALCULATE_CALL = CallDescription(
    name="calculate",
    input_format=_CALCULATION_INPUT_CODEC.format,
    result_format=_PREDICTION_RESULT_CODEC.format,
    state_inputs=(
        StateParameter(
            name="prepared",
            kind=_PREPARED_VALUES_KIND,
            use=StateUse.CONSUME,
        ),
    ),
    state_outputs=(),
    resources=(_CPU_THREAD_REQUIREMENT,),
    concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
    interruption=InterruptionSupport.NONE,
    progress=True,
)
_SUM_CALL = CallDescription(
    name="run",
    input_format=_SUM_INPUT_CODEC.format,
    result_format=_SUM_RESULT_CODEC.format,
    state_inputs=(),
    state_outputs=(),
    resources=(_CPU_THREAD_REQUIREMENT,),
    concurrency=ConcurrencyRequirement.PARALLEL,
    interruption=InterruptionSupport.NONE,
    progress=False,
)


def _sha256_digest(value: bytes) -> str:
    if type(value) is not bytes:
        raise TypeError("value must be bytes")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _binding_revision(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if type(part) is not str:
            raise TypeError("revision parts must be strings")
        encoded_part = part.encode("ascii")
        digest.update(len(encoded_part).to_bytes(8, "big"))
        digest.update(encoded_part)
    return "sha256:" + digest.hexdigest()


def _raise_scientific_rejection(
    error: science.ScientificInputRejected,
) -> NoReturn:
    raise ScientificRejection(
        code=error.code,
        message=error.message,
    ) from error


def _prepare(
    _model: science.WeightedSumModel,
    analysis: science.WeightedSumAnalysis,
    candidate: science.PreparationInput,
    _state: science.NoLiveState,
    _control: ScientificControl,
) -> ScientificReturn[science.PreparationResult]:
    try:
        prepared = science.prepare(
            analysis=analysis,
            candidate=candidate,
        )
    except science.ScientificInputRejected as error:
        _raise_scientific_rejection(error)

    return ScientificReturn(
        value=science.PreparationResult(count=prepared.value_count),
        states=(
            ReturnedState(
                name="prepared",
                kind=_PREPARED_VALUES_KIND,
                value=prepared,
                dispose=science.dispose_prepared,
            ),
        ),
        dispose_value=None,
    )


def _calculate(
    _model: science.WeightedSumModel,
    analysis: science.WeightedSumAnalysis,
    calculation: science.CalculationInput,
    state: science.CalculationState,
    control: ScientificControl,
) -> ScientificReturn[science.PredictionResult]:
    control.report_progress(
        ProgressEvent(
            stage="calculation",
            completed=0,
            total=1,
        )
    )
    prediction = science.calculate(
        analysis=analysis,
        calculation=calculation,
        state=state,
    )
    control.report_progress(
        ProgressEvent(
            stage="calculation",
            completed=1,
            total=1,
        )
    )

    return ScientificReturn(
        value=prediction,
        states=(),
        dispose_value=None,
    )


def _sum(
    _instance: None,
    _analysis: None,
    summation: science.SumInput,
    _state: science.NoLiveState,
    _control: ScientificControl,
) -> ScientificReturn[science.SumResult]:
    try:
        result = science.sum_values(summation=summation)
    except science.ScientificInputRejected as error:
        _raise_scientific_rejection(error)

    return ScientificReturn(
        value=result,
        states=(),
        dispose_value=None,
    )


def _describe_weighted_configuration(
    settings: science.WeightedSumSettings,
) -> ConfigurationDescription:
    encoded_settings = _SETTINGS_CODEC.encode(settings)
    return ConfigurationDescription(
        format=_SETTINGS_CODEC.format,
        digest=_sha256_digest(encoded_settings),
    )


def _describe_coefficients(
    coefficients: science.CoefficientSet,
) -> AssetDescription:
    encoded_coefficients = _COEFFICIENTS_CODEC.encode(coefficients)
    return AssetDescription(
        name="coefficients",
        media_type="application/vnd.nmr-provider-tools.dummy-coefficients+json",
        digest=_sha256_digest(encoded_coefficients),
        size_bytes=len(encoded_coefficients),
    )


def weighted_sum_binding(
    *,
    settings: science.WeightedSumSettings,
    coefficients: science.CoefficientSet,
) -> ImplementationBinding[science.WeightedSumModel, science.WeightedSumAnalysis]:
    """Bind immutable settings and coefficients to one weighted model revision."""
    configuration = _describe_weighted_configuration(settings)
    coefficient_asset = _describe_coefficients(coefficients)
    binding_revision = _binding_revision(
        "dummy.weighted-sum.v1",
        configuration.digest,
        coefficient_asset.digest,
    )

    implementation_description = ImplementationDescription(
        binding_id="dummy.weighted-sum",
        binding_revision=binding_revision,
        contract_version=1,
        configuration=configuration,
        assets=(coefficient_asset,),
        calls=(
            _PREPARE_CALL,
            _CALCULATE_CALL,
        ),
        resident_resources=(
            ResourceRequirement(
                kind="dummy-resident-data",
                amount=len(coefficients.values),
                unit="coefficient",
                selector="weighted-sum",
                exclusive=False,
            ),
        ),
        isolation=IsolationRequirement.SHARED_PROCESS,
    )

    return ImplementationBinding(
        description=implementation_description,
        instance=InstanceHooks(
            construct=partial(
                science.create_model,
                settings=settings,
                coefficients=coefficients,
            ),
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
                description=_PREPARE_CALL,
                input_codec=_PREPARATION_INPUT_CODEC,
                state_binding=_NO_LIVE_STATE_BINDING,
                scientific_call=_prepare,
                result_codec=_PREPARATION_RESULT_CODEC,
            ),
            CallBinding(
                description=_CALCULATE_CALL,
                input_codec=_CALCULATION_INPUT_CODEC,
                state_binding=_CALCULATION_STATE_BINDING,
                scientific_call=_calculate,
                result_codec=_PREDICTION_RESULT_CODEC,
            ),
        ),
    )


def sum_binding() -> ImplementationBinding[None, None]:
    """Bind the stateless sum without inventing model or analysis lifetimes."""
    sum_configuration = science.PlainSumConfiguration()
    encoded_configuration = _SUM_CONFIGURATION_CODEC.encode(sum_configuration)
    configuration = ConfigurationDescription(
        format=_SUM_CONFIGURATION_CODEC.format,
        digest=_sha256_digest(encoded_configuration),
    )

    implementation_description = ImplementationDescription(
        binding_id="dummy.sum",
        binding_revision=_binding_revision(
            "dummy.sum.v3",
            configuration.digest,
        ),
        contract_version=1,
        configuration=configuration,
        assets=(),
        calls=(_SUM_CALL,),
        resident_resources=(),
        isolation=IsolationRequirement.SHARED_PROCESS,
    )

    return ImplementationBinding(
        description=implementation_description,
        instance=None,
        analysis=None,
        calls=(
            CallBinding(
                description=_SUM_CALL,
                input_codec=_SUM_INPUT_CODEC,
                state_binding=_NO_LIVE_STATE_BINDING,
                scientific_call=_sum,
                result_codec=_SUM_RESULT_CODEC,
            ),
        ),
    )
