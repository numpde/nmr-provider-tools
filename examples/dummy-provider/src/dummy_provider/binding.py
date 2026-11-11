"""Register the dummy scientific calls, configuration and lifecycle hooks."""

from __future__ import annotations

from importlib.resources import files
import tomllib

import provider_core.contribution as contribution
import provider_core.contribution_declaration as declaration

from . import science


def _configured_cpu_resource() -> tuple[contribution.ResourceRequirement, contribution.AssetDescription]:
    resource_file = files(__package__).joinpath("resources.toml")
    requirement = contribution.ResourceRequirement(**tomllib.loads(resource_file.read_text(encoding="utf-8")))
    asset = declaration.json_asset(
        requirement,
        name="resources",
        format_id="dummy.resources.v1",
        media_type="application/vnd.nmr-provider-tools.dummy-resources+json",
    )
    return (requirement, asset)


def create_weighted_sum_binding(
    *,
    settings: science.WeightedSumSettings,
    coefficients: science.CoefficientSet,
) -> contribution.ImplementationBinding[
    science.WeightedSumModel,
    science.WeightedSumAnalysis,
]:
    """Bind immutable settings and coefficients to one weighted model revision."""
    (cpu_requirement, resource_asset) = _configured_cpu_resource()
    prepared_values_kind = "dummy.prepared-values.v1"

    def construct_model() -> science.WeightedSumModel:
        return science.create_model(settings=settings, coefficients=coefficients)

    def prepare(
        *,
        analysis: science.WeightedSumAnalysis,
        scientific_input: science.PreparationInput,
    ) -> contribution.ScientificReturn[science.PreparationResult]:
        prepared_values = science.prepare(
            analysis=analysis,
            candidate=scientific_input,
        )

        return contribution.ScientificReturn(
            value=science.PreparationResult(count=prepared_values.value_count),
            states=(
                contribution.ReturnedState(
                    name="prepared",
                    kind=prepared_values_kind,
                    value=prepared_values,
                    dispose=science.dispose_prepared,
                ),
            ),
        )

    def calculate(
        *,
        analysis: science.WeightedSumAnalysis,
        state: science.CalculationState,
        control: contribution.ScientificControl,
    ) -> contribution.ScientificReturn[science.PredictionResult]:
        control.report_progress(
            contribution.ProgressEvent(stage="calculation", completed=0, total=1)
        )
        prediction_result = science.calculate(analysis=analysis, state=state)
        control.report_progress(
            contribution.ProgressEvent(stage="calculation", completed=1, total=1)
        )

        return contribution.ScientificReturn(value=prediction_result)

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
    assets = (coefficient_asset, resource_asset)

    prepare_call = contribution.CallBinding.from_json_dataclasses(
        name="prepare",
        input_format="dummy.preparation-input.v1",
        result_format="dummy.preparation-result.v1",
        state_inputs=(),
        state_outputs=(
            contribution.StateOutput(name="prepared", kind=prepared_values_kind),
        ),
        resources=(cpu_requirement,),
        concurrency=contribution.ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=False,
        scientific_call=prepare,
    )
    calculate_call = contribution.CallBinding.from_json_dataclasses(
        name="calculate",
        result_format="dummy.prediction-result.v1",
        state_inputs=(
            contribution.StateParameter(
                name="prepared",
                kind=prepared_values_kind,
                use=contribution.StateUse.CONSUME,
            ),
        ),
        state_outputs=(),
        resources=(cpu_requirement,),
        concurrency=contribution.ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=True,
        scientific_call=calculate,
    )
    calls = (prepare_call, calculate_call)

    return contribution.ImplementationBinding.from_calls(
        binding_id="dummy.weighted-sum",
        binding_revision=declaration.binding_revision(
            implementation="dummy.weighted-sum.v2",
            configuration=configuration,
            assets=assets,
        ),
        contract_version=1,
        configuration=configuration,
        assets=assets,
        calls=calls,
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


def create_sum_binding() -> contribution.ImplementationBinding[None, None]:
    """Register a stateless sum with one independently callable operation."""
    (cpu_requirement, resource_asset) = _configured_cpu_resource()

    def run(
        *,
        scientific_input: science.SumInput,
    ) -> contribution.ScientificReturn[science.SumResult]:
        return contribution.ScientificReturn(
            value=science.sum_values(summation=scientific_input),
        )

    configuration = None
    assets = (resource_asset,)
    run_call = contribution.CallBinding.from_json_dataclasses(
        name="run",
        input_format="dummy.sum-input.v1",
        result_format="dummy.sum-result.v1",
        state_inputs=(),
        state_outputs=(),
        resources=(cpu_requirement,),
        concurrency=contribution.ConcurrencyRequirement.PARALLEL,
        interruption=contribution.InterruptionSupport.NONE,
        progress=False,
        scientific_call=run,
    )

    return contribution.ImplementationBinding.from_calls(
        binding_id="dummy.sum",
        binding_revision=declaration.binding_revision(
            implementation="dummy.sum.v5",
            configuration=configuration,
            assets=assets,
        ),
        contract_version=1,
        configuration=configuration,
        assets=assets,
        calls=(run_call,),
        resident_resources=(),
        isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=None,
        analysis=None,
    )
