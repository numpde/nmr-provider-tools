"""Bind the dummy science to the shared contribution interface."""

from __future__ import annotations

from functools import partial
from typing import Final, NoReturn

import provider_core.contribution as contribution
import provider_core.contribution_declaration as declaration

from . import science


__all__ = (
    "sum_binding",
    "weighted_sum_binding",
)


_PREPARED_VALUES_KIND: Final[str] = "dummy.prepared-values.v1"
_CPU_THREAD: Final[contribution.ResourceRequirement] = contribution.ResourceRequirement(
    kind="cpu",
    amount=1,
    unit="thread",
    selector="generic",
    exclusive=False,
)


def _raise_scientific_rejection(
    error: science.ScientificInputRejected,
) -> NoReturn:
    raise contribution.ScientificRejection(
        code=error.code,
        message=error.message,
    ) from error


def _prepare(
    _model: science.WeightedSumModel,
    analysis: science.WeightedSumAnalysis,
    candidate: science.PreparationInput,
    _state: science.NoLiveState,
    _control: contribution.ScientificControl,
) -> contribution.ScientificReturn[science.PreparationResult]:
    try:
        prepared = science.prepare(
            analysis=analysis,
            candidate=candidate,
        )
    except science.ScientificInputRejected as error:
        _raise_scientific_rejection(error)

    return contribution.ScientificReturn(
        value=science.PreparationResult(count=prepared.value_count),
        states=(
            contribution.ReturnedState(
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
    control: contribution.ScientificControl,
) -> contribution.ScientificReturn[science.PredictionResult]:
    control.report_progress(
        contribution.ProgressEvent(
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
        contribution.ProgressEvent(
            stage="calculation",
            completed=1,
            total=1,
        )
    )

    return contribution.ScientificReturn(
        value=prediction,
        states=(),
        dispose_value=None,
    )


def _sum(
    _instance: None,
    _analysis: None,
    summation: science.SumInput,
    _state: science.NoLiveState,
    _control: contribution.ScientificControl,
) -> contribution.ScientificReturn[science.SumResult]:
    try:
        result = science.sum_values(summation=summation)
    except science.ScientificInputRejected as error:
        _raise_scientific_rejection(error)

    return contribution.ScientificReturn(
        value=result,
        states=(),
        dispose_value=None,
    )


def weighted_sum_binding(
    *,
    settings: science.WeightedSumSettings,
    coefficients: science.CoefficientSet,
) -> contribution.ImplementationBinding[
    science.WeightedSumModel,
    science.WeightedSumAnalysis,
]:
    """Bind immutable settings and coefficients to one weighted model revision."""
    configuration = declaration.json_configuration(
        settings,
        format_id="dummy.settings.v1",
    )
    coefficient_asset = declaration.json_asset(
        coefficients,
        name="coefficients",
        format_id="dummy.coefficients.v1",
        media_type="application/vnd.nmr-provider-tools.dummy-coefficients+json",
    )
    assets = (coefficient_asset,)

    prepare_call = contribution.CallBinding.from_json_dataclasses(
        name="prepare",
        input_format="dummy.preparation-input.v1",
        result_format="dummy.preparation-result.v1",
        state_inputs=(),
        state_outputs=(
            contribution.StateOutput(
                name="prepared",
                kind=_PREPARED_VALUES_KIND,
            ),
        ),
        resources=(_CPU_THREAD,),
        concurrency=contribution.ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=False,
        scientific_call=_prepare,
    )
    calculate_call = contribution.CallBinding.from_json_dataclasses(
        name="calculate",
        input_format="dummy.calculation-input.v1",
        result_format="dummy.prediction-result.v1",
        state_inputs=(
            contribution.StateParameter(
                name="prepared",
                kind=_PREPARED_VALUES_KIND,
                use=contribution.StateUse.CONSUME,
            ),
        ),
        state_outputs=(),
        resources=(_CPU_THREAD,),
        concurrency=contribution.ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=True,
        scientific_call=_calculate,
    )
    calls = (prepare_call, calculate_call)

    return contribution.ImplementationBinding.from_calls(
        binding_id="dummy.weighted-sum",
        binding_revision=declaration.binding_revision(
            implementation="dummy.weighted-sum.v1",
            configuration=configuration,
            assets=assets,
        ),
        contract_version=1,
        configuration=configuration,
        assets=assets,
        calls=calls,
        resident_resources=(
            contribution.ResourceRequirement(
                kind="dummy-resident-data",
                amount=len(coefficients.values),
                unit="coefficient",
                selector="weighted-sum",
                exclusive=False,
            ),
        ),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=contribution.InstanceHooks(
            construct=partial(
                science.create_model,
                settings=settings,
                coefficients=coefficients,
            ),
            initialize=science.load_model,
            dispose=science.dispose_model,
        ),
        analysis=contribution.AnalysisHooks(
            construct=science.create_analysis,
            initialize=science.initialize_analysis,
            dispose=science.dispose_analysis,
        ),
    )


def sum_binding() -> contribution.ImplementationBinding[None, None]:
    """Bind the stateless sum without inventing model or analysis lifetimes."""
    configuration = declaration.json_configuration(
        science.PlainSumConfiguration(),
        format_id="dummy.sum-configuration.v1",
    )
    run_call = contribution.CallBinding.from_json_dataclasses(
        name="run",
        input_format="dummy.sum-input.v1",
        result_format="dummy.sum-result.v1",
        state_inputs=(),
        state_outputs=(),
        resources=(_CPU_THREAD,),
        concurrency=contribution.ConcurrencyRequirement.PARALLEL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=False,
        scientific_call=_sum,
    )

    return contribution.ImplementationBinding.from_calls(
        binding_id="dummy.sum",
        binding_revision=declaration.binding_revision(
            implementation="dummy.sum.v3",
            configuration=configuration,
            assets=(),
        ),
        contract_version=1,
        configuration=configuration,
        assets=(),
        calls=(run_call,),
        resident_resources=(),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=None,
        analysis=None,
    )
