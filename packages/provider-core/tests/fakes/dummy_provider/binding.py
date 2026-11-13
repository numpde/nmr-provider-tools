"""Bind the dummy science to the shared contribution interface."""

from __future__ import annotations

from dataclasses import dataclass

import provider_core.contribution as contribution
import provider_core.contribution_declaration as declaration
from provider_core.state_slots import (
    SlotStateBinding, StateInputSlot, StateKind, StateOutputSlot,
)

from . import science


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedSumBinding(contribution.ImplementationBinding[
    science.WeightedSumModel, science.WeightedSumAnalysis,
]):
    prepare: contribution.CallBinding[
        science.PreparationInput, contribution.NoState, science.PreparationResult,
    ]
    calculate: contribution.CallBinding[
        contribution.NoInput, science.CalculationState, science.PredictionResult,
    ]
    prepared_output: StateOutputSlot[science.PreparedValues]
    prepared_input: StateInputSlot[science.PreparedValues]


@dataclass(frozen=True, slots=True, kw_only=True)
class SumBinding(contribution.ImplementationBinding[None, None]):
    run: contribution.CallBinding[
        science.SumInput, contribution.NoState, science.SumResult,
    ]


_CPU_THREAD = contribution.ResourceRequirement(
    kind="cpu",
    amount=1,
    unit="thread",
    selector="generic",
    exclusive=False,
)


def weighted_sum_binding(
    *,
    settings: science.WeightedSumSettings,
    coefficients: science.CoefficientSet,
) -> WeightedSumBinding:
    """Bind immutable settings and coefficients to one weighted model revision."""
    prepared_kind = StateKind("dummy.prepared-values.v1", science.PreparedValues)
    prepared_output = prepared_kind.output(
        name="normalized", dispose=science.dispose_prepared,
    )
    prepared_input = prepared_kind.input(
        name="candidate", use=contribution.StateUse.CONSUME,
    )
    calculation_state = SlotStateBinding(
        value_type=science.CalculationState,
        inputs=(prepared_input,),
        assemble=lambda values: science.CalculationState(
            prepared=prepared_input.read(values),
        ),
    )

    def construct_model() -> science.WeightedSumModel:
        return science.create_model(
            settings=settings,
            coefficients=coefficients,
        )

    def prepare(
        context: contribution.CallContext[
            science.WeightedSumModel, science.WeightedSumAnalysis,
            science.PreparationInput, contribution.NoState,
        ],
    ) -> contribution.ScientificReturn[science.PreparationResult]:
        prepared = science.prepare(
            analysis=context.analysis,
            candidate=context.scientific_input,
        )

        return contribution.ScientificReturn(
            value=science.PreparationResult(count=prepared.value_count),
            states=(prepared_output.returned(prepared),),
        )

    def calculate(
        context: contribution.CallContext[
            science.WeightedSumModel, science.WeightedSumAnalysis,
            contribution.NoInput, science.CalculationState,
        ],
    ) -> contribution.ScientificReturn[science.PredictionResult]:
        control = context.require_control()
        control.report_progress(
            contribution.ProgressEvent(
                stage="calculation",
                completed=0,
                total=1,
            )
        )
        prediction = science.calculate(
            analysis=context.analysis,
            state=context.state,
        )
        control.report_progress(
            contribution.ProgressEvent(
                stage="calculation",
                completed=1,
                total=1,
            )
        )

        return contribution.ScientificReturn(value=prediction)

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
        input_type=science.PreparationInput,
        state_type=contribution.NoState,
        result_type=science.PreparationResult,
        input_format="dummy.preparation-input.v1",
        result_format="dummy.preparation-result.v1",
        state_outputs=(prepared_output,),
        resources=(_CPU_THREAD,),
        concurrency=contribution.ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=False,
        scientific_call=prepare,
    )
    calculate_call = contribution.CallBinding.from_json_dataclasses(
        name="calculate",
        input_type=contribution.NoInput,
        state_type=science.CalculationState,
        result_type=science.PredictionResult,
        result_format="dummy.prediction-result.v1",
        state_binding=calculation_state,
        state_outputs=(),
        resources=(_CPU_THREAD,),
        concurrency=contribution.ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=True,
        scientific_call=calculate,
    )
    return WeightedSumBinding(
        identity=contribution.ConfiguredIdentity(
            binding_id="dummy.weighted-sum",
            implementation="dummy.weighted-sum.v2",
            contract_version=1,
            configuration=configuration,
            assets=assets,
        ),
        prepare=prepare_call,
        calculate=calculate_call,
        prepared_output=prepared_output,
        prepared_input=prepared_input,
        resident_resources=(),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=contribution.InstanceHooks(
            construct=construct_model,
            initialize=science.load_model,
            dispose=science.dispose_model,
        ),
        analysis=contribution.AnalysisHooks(
            construct=science.create_analysis,
            initialize=science.initialize_analysis,
            dispose=science.dispose_analysis,
        ),
    )


def sum_binding() -> SumBinding:
    """Bind the stateless sum without inventing model or analysis lifetimes."""

    def run(scientific_input: science.SumInput) -> science.SumResult:
        return science.sum_values(summation=scientific_input)

    configuration = None
    run_call = contribution.CallBinding.from_function(
        name="run",
        input_type=science.SumInput,
        result_type=science.SumResult,
        input_format="dummy.sum-input.v1",
        result_format="dummy.sum-result.v1",
        resources=(_CPU_THREAD,),
        concurrency=contribution.ConcurrencyRequirement.PARALLEL,
        function=run,
    )

    return SumBinding(
        identity=contribution.ConfiguredIdentity(
            binding_id="dummy.sum",
            implementation="dummy.sum.v5",
            contract_version=1,
            configuration=configuration,
            assets=(),
        ),
        run=run_call,
        resident_resources=(),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=None,
        analysis=None,
    )
