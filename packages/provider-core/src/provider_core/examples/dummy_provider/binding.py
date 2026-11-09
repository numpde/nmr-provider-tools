"""Bind the dummy science to the shared contribution interface."""

from __future__ import annotations

import hashlib
from functools import partial
from typing import Final, NoReturn

from provider_core.contribution import (
    AnalysisHooks,
    AssetDescription,
    CallBinding,
    ConcurrencyRequirement,
    ConfigurationDescription,
    ImplementationBinding,
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


_PREPARED_VALUES_KIND: Final[str] = "dummy.prepared-values.v1"
_CPU_THREAD: Final[ResourceRequirement] = ResourceRequirement(
    kind="cpu",
    amount=1,
    unit="thread",
    selector="generic",
    exclusive=False,
)


def _sha256_digest(content: bytes) -> str:
    if type(content) is not bytes:
        raise TypeError("content must be bytes")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _binding_revision(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if type(part) is not str:
            raise TypeError("revision parts must be strings")
        encoded_part = part.encode("ascii")
        digest.update(len(encoded_part).to_bytes(8, "big"))
        digest.update(encoded_part)
    return "sha256:" + digest.hexdigest()


def _configuration_description[T](
    value: T,
    *,
    format_id: str,
) -> ConfigurationDescription:
    codec = JsonDataclassCodec(
        format=format_id,
        value_type=type(value),
    )
    encoded_value = codec.encode(value)
    return ConfigurationDescription(
        format=codec.format,
        digest=_sha256_digest(encoded_value),
    )


def _asset_description[T](
    value: T,
    *,
    name: str,
    format_id: str,
    media_type: str,
) -> AssetDescription:
    codec = JsonDataclassCodec(
        format=format_id,
        value_type=type(value),
    )
    encoded_value = codec.encode(value)
    return AssetDescription(
        name=name,
        media_type=media_type,
        digest=_sha256_digest(encoded_value),
        size_bytes=len(encoded_value),
    )


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
    control.report_progress(ProgressEvent(stage="calculation", completed=0, total=1))
    prediction = science.calculate(
        analysis=analysis,
        calculation=calculation,
        state=state,
    )
    control.report_progress(ProgressEvent(stage="calculation", completed=1, total=1))

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


def weighted_sum_binding(
    *,
    settings: science.WeightedSumSettings,
    coefficients: science.CoefficientSet,
) -> ImplementationBinding[science.WeightedSumModel, science.WeightedSumAnalysis]:
    """Bind immutable settings and coefficients to one weighted model revision."""
    configuration = _configuration_description(
        settings,
        format_id="dummy.settings.v1",
    )
    coefficient_asset = _asset_description(
        coefficients,
        name="coefficients",
        format_id="dummy.coefficients.v1",
        media_type="application/vnd.nmr-provider-tools.dummy-coefficients+json",
    )

    prepare_call = CallBinding.from_json_dataclasses(
        name="prepare",
        input_format="dummy.preparation-input.v1",
        result_format="dummy.preparation-result.v1",
        state_inputs=(),
        state_outputs=(
            StateOutput(
                name="prepared",
                kind=_PREPARED_VALUES_KIND,
            ),
        ),
        resources=(_CPU_THREAD,),
        concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=InterruptionSupport.NONE,
        progress=False,
        scientific_call=_prepare,
    )
    calculate_call = CallBinding.from_json_dataclasses(
        name="calculate",
        input_format="dummy.calculation-input.v1",
        result_format="dummy.prediction-result.v1",
        state_inputs=(
            StateParameter(
                name="prepared",
                kind=_PREPARED_VALUES_KIND,
                use=StateUse.CONSUME,
            ),
        ),
        state_outputs=(),
        resources=(_CPU_THREAD,),
        concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=InterruptionSupport.NONE,
        progress=True,
        scientific_call=_calculate,
    )
    calls = (prepare_call, calculate_call)

    return ImplementationBinding.from_calls(
        binding_id="dummy.weighted-sum",
        binding_revision=_binding_revision(
            "dummy.weighted-sum.v1",
            configuration.digest,
            coefficient_asset.digest,
        ),
        contract_version=1,
        configuration=configuration,
        assets=(coefficient_asset,),
        calls=calls,
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
    )


def sum_binding() -> ImplementationBinding[None, None]:
    """Bind the stateless sum without inventing model or analysis lifetimes."""
    configuration = _configuration_description(
        science.PlainSumConfiguration(),
        format_id="dummy.sum-configuration.v1",
    )
    run_call = CallBinding.from_json_dataclasses(
        name="run",
        input_format="dummy.sum-input.v1",
        result_format="dummy.sum-result.v1",
        state_inputs=(),
        state_outputs=(),
        resources=(_CPU_THREAD,),
        concurrency=ConcurrencyRequirement.PARALLEL,
        interruption=InterruptionSupport.NONE,
        progress=False,
        scientific_call=_sum,
    )

    return ImplementationBinding.from_calls(
        binding_id="dummy.sum",
        binding_revision=_binding_revision(
            "dummy.sum.v3",
            configuration.digest,
        ),
        contract_version=1,
        configuration=configuration,
        assets=(),
        calls=(run_call,),
        resident_resources=(),
        isolation=IsolationRequirement.SHARED_PROCESS,
        instance=None,
        analysis=None,
    )
