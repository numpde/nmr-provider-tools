"""Scientific records, operations and state lifetimes for the dummy provider.

Portable inputs and results use immutable records. A weighted model persists
across analyses, each analysis owns its accepted preparation, and calculation
consumes that preparation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import mock_numpy as np

from provider_core.contribution_errors import ScientificRejection


def _finite_scalar(*, numeric_scalar: object, field_description: str) -> float:
    if not (type(numeric_scalar) in (expected_type := (int, float))):
        raise TypeError(f"{field_description=}; {type(numeric_scalar)=} ≠ {expected_type=}")

    if not (np.isfinite(numeric_scalar)):
        raise ValueError(f"{field_description} must be a finite number")

    return float(numeric_scalar)


def _finite_samples(*, numeric_samples: object, field_description: str) -> tuple[float, ...]:
    if not (type(numeric_samples) is (expected_type := tuple)):
        raise TypeError(f"{field_description=}; {type(numeric_samples)=} ≠ {expected_type=}")

    # repo-check: allow-tuple-generator -- The scientific records retain immutable numerical sequences.
    return tuple(
        _finite_scalar(
            numeric_scalar=sample_number,
            field_description=f"{field_description}[{sample_index}]",
        )
        for (sample_index, sample_number) in enumerate(numeric_samples)
    )


class ScientificInputRejected(ScientificRejection):
    """The portable input is valid but scientifically unacceptable."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedSumSettings:
    scale: float
    offset: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "scale", _finite_scalar(numeric_scalar=self.scale, field_description="scale"))
        object.__setattr__(self, "offset", _finite_scalar(numeric_scalar=self.offset, field_description="offset"))


@dataclass(frozen=True, slots=True, kw_only=True)
class CoefficientSet:
    weights: tuple[float, ...]

    def __post_init__(self) -> None:
        coefficient_weights = _finite_samples(
            numeric_samples=self.weights,
            field_description="coefficient weights",
        )

        if not (coefficient_weights):
            raise ValueError("coefficient weights must not be empty")

        object.__setattr__(self, "weights", coefficient_weights)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationInput:
    samples: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "samples", _finite_samples(numeric_samples=self.samples, field_description="preparation samples"))


@dataclass(frozen=True, slots=True, kw_only=True)
class SumInput:
    samples: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "samples", _finite_samples(numeric_samples=self.samples, field_description="summation samples"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationResult:
    count: int

    def __post_init__(self) -> None:
        if not (type((actual_count := self.count)) is (expected_type := int)):
            raise TypeError(f"{type(actual_count)=} ≠ {expected_type=}")

        if not (0 < self.count):
            raise ValueError("count must be a positive integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class PredictionResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", _finite_scalar(numeric_scalar=self.total, field_description="prediction total"))

        if not (type((actual_count := self.count)) is (expected_type := int)):
            raise TypeError(f"{type(actual_count)=} ≠ {expected_type=}")

        if not (0 < self.count):
            raise ValueError("count must be a positive integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class SumResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", _finite_scalar(numeric_scalar=self.total, field_description="sum total"))

        if not (type((actual_count := self.count)) is (expected_type := int)):
            raise TypeError(f"{type(actual_count)=} ≠ {expected_type=}")

        if not (0 < self.count):
            raise ValueError("count must be a positive integer")


@dataclass(slots=True, kw_only=True)
class WeightedSumModel:
    settings: WeightedSumSettings
    coefficients: CoefficientSet
    loaded_coefficients: tuple[float, ...] | None = field(default=None, init=False, repr=False)


@dataclass(slots=True, kw_only=True)
class WeightedSumAnalysis:
    model: WeightedSumModel
    is_open: bool = field(default=False, init=False)
    accepted_preparation: PreparedSamples | None = field(default=None, init=False, repr=False)


@dataclass(slots=True, kw_only=True)
class PreparedSamples:
    """Worker-local samples owned by exactly one scientific analysis."""

    analysis: WeightedSumAnalysis
    samples: tuple[float, ...] | None = field(repr=False)

    def __post_init__(self) -> None:
        normalized_samples = _finite_samples(numeric_samples=self.samples, field_description="prepared samples")

        if not (normalized_samples):
            raise ValueError("prepared samples must not be empty")

        self.samples = normalized_samples

    @property
    def sample_count(self) -> int:
        if self.samples is None:
            raise RuntimeError("The prepared samples have already been consumed or disposed")

        return len(self.samples)

    def consume(self, *, analysis: WeightedSumAnalysis) -> tuple[float, ...]:
        if not (self.analysis is analysis):
            raise RuntimeError("The prepared samples belong to another analysis")

        if not (analysis.accepted_preparation is self):
            raise RuntimeError("The prepared samples are not this analysis's accepted preparation")

        if self.samples is None:
            raise RuntimeError("The prepared samples have already been consumed or disposed")

        prepared_samples = self.samples
        analysis.accepted_preparation = None
        self.samples = None

        return prepared_samples

    def dispose(self) -> None:
        if self.analysis.accepted_preparation is self:
            self.analysis.accepted_preparation = None

        self.samples = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CalculationState:
    prepared: PreparedSamples


def create_model(
    *,
    settings: WeightedSumSettings,
    coefficients: CoefficientSet,
) -> WeightedSumModel:
    return WeightedSumModel(settings=settings, coefficients=coefficients)


def load_model(model: WeightedSumModel) -> None:
    if not (model.loaded_coefficients is None):
        raise RuntimeError("The model coefficients are already loaded")

    model.loaded_coefficients = model.coefficients.weights


def dispose_model(model: WeightedSumModel) -> None:
    model.loaded_coefficients = None


def create_analysis(model: WeightedSumModel) -> WeightedSumAnalysis:
    return WeightedSumAnalysis(model=model)


def initialize_analysis(analysis: WeightedSumAnalysis) -> None:
    if analysis.is_open:
        raise RuntimeError("The analysis is already initialized")

    if analysis.model.loaded_coefficients is None:
        raise RuntimeError("Cannot initialize an analysis before loading model coefficients")

    analysis.is_open = True


def prepare(
    analysis: WeightedSumAnalysis,
    candidate: PreparationInput,
) -> PreparedSamples:
    """Reject one candidate without discarding the analysis itself."""

    if not (type(candidate) is (expected_type := PreparationInput)):
        raise TypeError(f"{type(candidate)=} ≠ {expected_type=}")

    if not (analysis.is_open):
        raise RuntimeError("Cannot prepare input for a closed analysis")

    if not (candidate.samples):
        raise ScientificInputRejected(
            "empty_input",
            "Preparation input must contain at least one sample",
        )

    if not (analysis.accepted_preparation is None):
        raise ScientificInputRejected(
            "already_prepared",
            "Calculate or discard the accepted preparation before preparing another",
        )

    loaded_coefficients = analysis.model.loaded_coefficients

    if loaded_coefficients is None:
        raise RuntimeError("Cannot prepare input because model coefficients are not loaded")

    actual_sample_count = len(candidate.samples)
    expected_sample_count = len(loaded_coefficients)

    if actual_sample_count != expected_sample_count:
        raise ScientificInputRejected(
            "wrong_count",
            f"{actual_sample_count = }, {expected_sample_count = }",
        )

    prepared_samples = PreparedSamples(analysis=analysis, samples=candidate.samples)
    analysis.accepted_preparation = prepared_samples

    return prepared_samples


def calculate(
    analysis: WeightedSumAnalysis,
    state: CalculationState,
) -> PredictionResult:
    if not (type(state) is (expected_type := CalculationState)):
        raise TypeError(f"{type(state)=} ≠ {expected_type=}")

    if not (analysis.is_open):
        raise RuntimeError("Cannot calculate for a closed analysis")

    loaded_coefficients = analysis.model.loaded_coefficients

    if loaded_coefficients is None:
        raise RuntimeError("Cannot calculate because model coefficients are not loaded")

    prepared_samples = state.prepared.consume(analysis=analysis)

    # State is consumed before numerical work. A later failure does not make the
    # old state reference usable again.
    weighted_sum = math.fsum(
        coefficient * sample_number
        for (coefficient, sample_number) in zip(
            loaded_coefficients,
            prepared_samples,
            strict=True,
        )
    )

    prediction_total = analysis.model.settings.offset + analysis.model.settings.scale * weighted_sum

    if not (np.isfinite(prediction_total)):
        raise ArithmeticError("The weighted prediction total is not finite")

    return PredictionResult(total=prediction_total, count=len(prepared_samples))


def sum_values(summation: SumInput) -> SumResult:
    if not (type(summation) is (expected_type := SumInput)):
        raise TypeError(f"{type(summation)=} ≠ {expected_type=}")

    if not (summation.samples):
        raise ScientificInputRejected(
            "empty_input",
            "Sum input must contain at least one sample",
        )

    total = math.fsum(summation.samples)

    if not (np.isfinite(total)):
        raise ArithmeticError("The sum total is not finite")

    return SumResult(total=total, count=len(summation.samples))


def dispose_prepared(prepared: PreparedSamples) -> None:
    """Release retained values without changing the analysis lifetime."""
    prepared.dispose()


def dispose_analysis(analysis: WeightedSumAnalysis) -> None:
    accepted_preparation = analysis.accepted_preparation

    if accepted_preparation is not None:
        dispose_prepared(accepted_preparation)

    analysis.is_open = False
