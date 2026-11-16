"""Configure scientific classes; the shared host owns their execution lifetimes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

import provider_core.scientific as sdk

from . import science


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedSumConfig:
    settings: science.WeightedSumSettings
    coefficients: science.CoefficientSet


class WeightedAnalysis(sdk.Analysis["WeightedSum"]):
    _value: science.WeightedSumAnalysis | None = None

    @property
    def value(self) -> science.WeightedSumAnalysis:
        if self._value is None:
            raise RuntimeError("Initialize the weighted analysis before using its scientific session")

        return self._value

    def initialize(self) -> None:
        self._value = science.create_analysis(model=self.contribution.model)

        science.initialize_analysis(analysis=self.value)

    def close(self) -> None:
        if self._value is not None:
            science.dispose_analysis(analysis=self._value)


class WeightedSum(sdk.Contribution):
    def __init__(self, *, configuration: WeightedSumConfig) -> None:
        super().__init__()
        self.configuration = configuration

    analysis_type = WeightedAnalysis
    _model: science.WeightedSumModel | None = None
    prepared_state_type = sdk.StateType(value_type=science.PreparedSamples)
    normalized = prepared_state_type.output(dispose=science.dispose_prepared)
    candidate = prepared_state_type.input(use=sdk.StateUse.CONSUME)

    @property
    def model(self) -> science.WeightedSumModel:
        if self._model is None:
            raise RuntimeError("Initialize the weighted contribution before using its model")

        return self._model

    def initialize(self, *, assets: sdk.AssetAccess) -> None:
        # Retain the empty model before loading can acquire resources or fail.
        # close can then dispose that same partial model after a loading failure.
        self._model = science.create_model(
            settings=self.configuration.settings,
            coefficients=self.configuration.coefficients,
        )

        science.load_model(model=self.model)

    def close(self) -> None:
        if self._model is not None:
            science.dispose_model(model=self._model)

    @sdk.operation(state_outputs=(normalized,))
    def prepare(
        self,
        *,
        analysis: WeightedAnalysis,
        candidate: science.PreparationInput,
    ) -> sdk.ScientificReturn[science.PreparationResult]:
        prepared = science.prepare(analysis=analysis.value, candidate=candidate)

        return sdk.ScientificReturn(
            value=science.PreparationResult(count=prepared.sample_count),
            states=(self.normalized.returned(value=prepared),),
        )

    @sdk.operation(state_inputs=(candidate,), progress=True)
    def calculate(
        self,
        *,
        analysis: WeightedAnalysis,
        candidate: science.PreparedSamples,
        control: sdk.ScientificControl,
    ) -> science.PredictionResult:
        control.report_progress(event=sdk.ProgressEvent(stage="calculation", completed=0, total=1))

        prediction_result = science.calculate(
            analysis=analysis.value,
            state=science.CalculationState(prepared=candidate),
        )

        control.report_progress(event=sdk.ProgressEvent(stage="calculation", completed=1, total=1))

        return prediction_result


class Sum(sdk.Contribution):
    @sdk.operation(concurrency=sdk.ConcurrencyRequirement.PARALLEL)
    def run(self, *, summation: science.SumInput) -> science.SumResult:
        return science.sum_values(summation=summation)


# This component example's caller supplies the host and operation policy.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = Path("config/deployments/localhost/provider.toml")


def read_resource_requirements() -> frozenset[sdk.ResourceRequirement]:
    """Read this deployment's declared scientific call requirements."""

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        configuration = tomllib.load(source)

    return frozenset(
        sdk.ResourceRequirement(**requirement)
        for requirement in configuration["execution"]["resource_requirement"]
    )
