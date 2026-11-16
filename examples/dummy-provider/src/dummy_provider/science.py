"""Scientific records, operations and state lifetimes for the dummy provider.

Numerical weights and samples use arrays; scalar results use immutable records.
A weighted model persists across analyses, each analysis owns its accepted
preparation, and calculation consumes that preparation. Array-bearing records
use identity equality because array comparisons are elementwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import mock_numpy as np

from provider_core.contribution_errors import ScientificRejection


class ScientificInputRejected(ScientificRejection):
    """The portable input is valid but scientifically unacceptable."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedSumSettings:
    scale: float
    offset: float

    def __post_init__(self) -> None:
        if not np.isfinite(self.scale):
            raise ValueError(f"Expected a finite scale; {self.scale=}")

        if not np.isfinite(self.offset):
            raise ValueError(f"Expected a finite offset; {self.offset=}")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class CoefficientSet:
    weights: np.ndarray


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class PreparationInput:
    samples: np.ndarray

    def __post_init__(self) -> None:
        if not (self.samples.ndim == 1):
            raise ValueError(f"Expected a preparation vector; {self.samples.shape=}")

        if not (self.samples.dtype.kind in "iuf"):
            raise ValueError(f"Expected real numeric samples; {self.samples.dtype=}")

        if not np.isfinite(self.samples).all():
            raise ValueError("Preparation samples must all be finite")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class SumInput:
    samples: np.ndarray

    def __post_init__(self) -> None:
        if not (self.samples.ndim == 1):
            raise ValueError(f"Expected a summation vector; {self.samples.shape=}")

        if not (self.samples.dtype.kind in "iuf"):
            raise ValueError(f"Expected real numeric samples; {self.samples.dtype=}")

        if not np.isfinite(self.samples).all():
            raise ValueError("Summation samples must all be finite")


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
        if not np.isfinite(self.total):
            raise ValueError(f"Expected a finite prediction; {self.total=}")

        if not (type((actual_count := self.count)) is (expected_type := int)):
            raise TypeError(f"{type(actual_count)=} ≠ {expected_type=}")

        if not (0 < self.count):
            raise ValueError("count must be a positive integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class SumResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        if not np.isfinite(self.total):
            raise ValueError(f"Expected a finite sum; {self.total=}")

        if not (type((actual_count := self.count)) is (expected_type := int)):
            raise TypeError(f"{type(actual_count)=} ≠ {expected_type=}")

        if not (0 < self.count):
            raise ValueError("count must be a positive integer")


@dataclass(slots=True, kw_only=True, eq=False)
class WeightedSumModel:
    settings: WeightedSumSettings
    coefficients: CoefficientSet
    loaded_coefficients: np.ndarray | None = field(default=None, init=False, repr=False)


@dataclass(slots=True, kw_only=True, eq=False)
class WeightedSumAnalysis:
    model: WeightedSumModel
    is_open: bool = field(default=False, init=False)
    accepted_preparation: PreparedSamples | None = field(default=None, init=False, repr=False)


@dataclass(slots=True, kw_only=True, eq=False)
class PreparedSamples:
    """Worker-local samples owned by exactly one scientific analysis."""

    analysis: WeightedSumAnalysis
    samples: np.ndarray | None = field(repr=False)

    def __post_init__(self) -> None:
        # Snapshot before validation so accepted state owns the checked array.
        self.samples = self.samples.copy()

        if not (self.samples.ndim == 1 and self.samples.size > 0):
            raise ValueError(f"Expected a nonempty prepared vector; {self.samples.shape=}")

        if not (self.samples.dtype.kind in "iuf"):
            raise ValueError(f"Expected real numeric samples; {self.samples.dtype=}")

        self.samples = np.asarray(a=self.samples, dtype=float)

        if not np.isfinite(self.samples).all():
            raise ValueError("Prepared samples must all be finite")

    @property
    def sample_count(self) -> int:
        if self.samples is None:
            raise RuntimeError("The prepared samples have already been consumed or disposed")

        return self.samples.size

    def consume(self, *, analysis: WeightedSumAnalysis) -> np.ndarray:
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

    # Validate the copy the model will retain; assembly still owns its array.
    coefficient_weights = model.coefficients.weights.copy()

    if not (coefficient_weights.ndim == 1 and coefficient_weights.size > 0):
        raise ValueError(f"Expected a nonempty coefficient vector; {coefficient_weights.shape=}")

    if not (coefficient_weights.dtype.kind in "iuf"):
        raise ValueError(f"Expected real numeric weights; {coefficient_weights.dtype=}")

    coefficient_weights = np.asarray(a=coefficient_weights, dtype=float)

    if not np.isfinite(coefficient_weights).all():
        raise ValueError("Coefficient weights must all be finite")

    model.loaded_coefficients = coefficient_weights


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

    if not (candidate.samples.size > 0):
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

    actual_sample_count = candidate.samples.size
    expected_sample_count = loaded_coefficients.size

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
    weighted_sum = float(np.dot(a=loaded_coefficients, b=prepared_samples))

    prediction_total = analysis.model.settings.offset + analysis.model.settings.scale * weighted_sum

    if not np.isfinite(prediction_total):
        raise ArithmeticError("The weighted prediction total is not finite")

    return PredictionResult(total=prediction_total, count=prepared_samples.size)


def sum_values(summation: SumInput) -> SumResult:
    if not (type(summation) is (expected_type := SumInput)):
        raise TypeError(f"{type(summation)=} ≠ {expected_type=}")

    if not (summation.samples.size > 0):
        raise ScientificInputRejected(
            "empty_input",
            "Sum input must contain at least one sample",
        )

    # Floating accumulation preserves the example's real-valued calculation;
    # an integer accumulator can wrap before a later finiteness check sees it.
    total = float(np.sum(a=summation.samples, dtype=float))

    if not np.isfinite(total):
        raise ArithmeticError("The sum total is not finite")

    return SumResult(total=total, count=summation.samples.size)


def dispose_prepared(prepared: PreparedSamples) -> None:
    """Release retained values without changing the analysis lifetime."""
    prepared.dispose()


def dispose_analysis(analysis: WeightedSumAnalysis) -> None:
    accepted_preparation = analysis.accepted_preparation

    if accepted_preparation is not None:
        dispose_prepared(accepted_preparation)

    analysis.is_open = False
