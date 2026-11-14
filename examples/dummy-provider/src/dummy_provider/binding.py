"""Configure scientific classes; the shared host owns their execution lifetimes."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import tomllib

import provider_core.scientific as sdk
from provider_core.embedded_host import EmbeddedHost, ResourceAllocation

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
    prepared_kind = sdk.StateKind(name="dummy.prepared-values.v1", value_type=science.PreparedValues)
    prepared_output = prepared_kind.output(name="normalized", dispose=science.dispose_prepared)
    prepared_input = prepared_kind.input(name="candidate", use=sdk.StateUse.CONSUME)

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

    @sdk.operation(state_outputs=(prepared_output,))
    def prepare(
        self, *, analysis: WeightedAnalysis, candidate: science.PreparationInput,
    ) -> sdk.ScientificReturn[science.PreparationResult]:
        prepared = science.prepare(analysis=analysis.value, candidate=candidate)
        return sdk.ScientificReturn(
            value=science.PreparationResult(count=prepared.value_count),
            states=(self.prepared_output.returned(value=prepared),),
        )

    @sdk.operation(state_inputs=(prepared_input,), progress=True)
    def calculate(
        self, *, analysis: WeightedAnalysis, candidate: science.PreparedValues,
        control: sdk.ScientificControl,
    ) -> science.PredictionResult:
        control.report_progress(event=sdk.ProgressEvent(stage="calculation", completed=0, total=1))
        result = science.calculate(
            analysis=analysis.value,
            state=science.CalculationState(prepared=candidate),
        )
        control.report_progress(event=sdk.ProgressEvent(stage="calculation", completed=1, total=1))
        return result


class Sum(sdk.Contribution):
    @sdk.operation(concurrency=sdk.ConcurrencyRequirement.PARALLEL)
    def run(self, *, summation: science.SumInput) -> science.SumResult:
        return science.sum_values(summation=summation)


def _configured_cpu_resource() -> sdk.ResourceRequirement:
    resource_file = files(anchor=__package__).joinpath("resources.toml")
    return sdk.ResourceRequirement(**tomllib.loads(resource_file.read_text(encoding="utf-8")))


def create_weighted_sum_binding(
    *, host: EmbeddedHost, allocation: ResourceAllocation,
    settings: science.WeightedSumSettings, coefficients: science.CoefficientSet,
) -> sdk.HostedContribution:
    """Provider-owned assembly helper; adoption creates no live science.

    Callers explicitly invoke this helper. Its name has no SDK meaning; the
    exported methods and lifecycle contract belong to WeightedSum.
    """
    return host.adopt(
        contribution=WeightedSum(configuration=WeightedSumConfig(settings=settings, coefficients=coefficients)),
        allocation=allocation,
        requirements=(_configured_cpu_resource(),),
    )


def create_sum_binding(*, host: EmbeddedHost, allocation: ResourceAllocation) -> sdk.HostedContribution:
    """Provider-owned assembly for the independently callable stateless sum."""
    return host.adopt(
        contribution=Sum(), allocation=allocation,
        requirements=(_configured_cpu_resource(),),
    )
