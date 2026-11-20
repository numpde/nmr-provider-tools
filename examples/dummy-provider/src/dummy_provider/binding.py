"""Configure scientific classes; the shared host owns their execution lifetimes."""

from __future__ import annotations

from dataclasses import dataclass

import provider_core.scientific as sdk

from . import science
from .payloads import SamplesCodec


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedSumConfig:
    settings: science.WeightedSumSettings
    coefficients: science.CoefficientSet


class WeightedAnalysis(sdk.Analysis["WeightedSum"]):
    _scientific_session: science.WeightedSumAnalysis | None = None

    @property
    def scientific_session(self) -> science.WeightedSumAnalysis:
        if not (self._scientific_session is not None):
            raise RuntimeError(f"{self._scientific_session=} should be set before using the weighted analysis")

        return self._scientific_session

    def initialize(self) -> None:
        self._scientific_session = science.create_analysis(model=self.contribution.model)

        science.initialize_analysis(analysis=self.scientific_session)

    def close(self) -> None:
        if self._scientific_session is not None:
            science.dispose_analysis(analysis=self._scientific_session)


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

    @sdk.operation(
        state_outputs=(normalized,),
        input_codec=SamplesCodec(value_type=science.PreparationInput),
    )
    def prepare(
        self,
        *,
        analysis: WeightedAnalysis,
        candidate: science.PreparationInput,
    ) -> sdk.ScientificReturn[science.PreparationResult]:
        prepared = science.prepare(analysis=analysis.scientific_session, candidate=candidate)

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
            analysis=analysis.scientific_session,
            state=science.CalculationState(prepared=candidate),
        )

        control.report_progress(event=sdk.ProgressEvent(stage="calculation", completed=1, total=1))

        return prediction_result


class Sum(sdk.Contribution):
    @sdk.operation(
        concurrency=sdk.ConcurrencyRequirement.PARALLEL,
        input_codec=SamplesCodec(value_type=science.SumInput),
    )
    def run(self, *, summation: science.SumInput) -> science.SumResult:
        return science.sum_values(summation=summation)
