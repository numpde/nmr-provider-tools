"""Scientific records, operations and state lifetimes for the dummy provider.

Numerical weights and samples use arrays; scalar results use immutable records.
A weighted model persists across analyses, each analysis owns its accepted
preparation, and calculation consumes that preparation. Array-bearing records
use identity equality because array comparisons are elementwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import auto

import mock_numpy as np

from provider_core.contribution_errors import ErrorCode, ScientificRejection
from provider_core.type_requirements import require_instance, require_type


class ScientificInputRejected(ScientificRejection):
    """The portable input is valid but scientifically unacceptable."""

    class Code(ErrorCode):
        EMPTY_INPUT = auto()
        ALREADY_PREPARED = auto()
        WRONG_COUNT = auto()

    def __init__(self, message: str, *, code: ScientificInputRejected.Code) -> None:
        require_instance(code, self.Code)

        super().__init__(code=code, message=message)


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedSumSettings:
    scale: float
    offset: float

    def __post_init__(self) -> None:
        if not np.isfinite(self.scale):
            raise ValueError(f"{self.scale=} should be finite")

        if not np.isfinite(self.offset):
            raise ValueError(f"{self.offset=} should be finite")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class CoefficientSet:
    weights: np.ndarray


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class PreparationInput:
    samples: np.ndarray

    def __post_init__(self) -> None:
        if not (self.samples.ndim == 1):
            raise ValueError(f"{self.samples.shape=} should be a vector")

        if not (self.samples.dtype.kind in "iuf"):
            raise ValueError(f"{self.samples.dtype=} should be real numeric")

        sample_finiteness = np.isfinite(self.samples)

        if not sample_finiteness.all():
            sample_index = next(index for index in range(self.samples.size) if not (sample_finiteness[index]))
            sample = self.samples[sample_index]

            raise ValueError(f"{sample=} at {sample_index=} should be finite")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class SumInput:
    samples: np.ndarray

    def __post_init__(self) -> None:
        if not (self.samples.ndim == 1):
            raise ValueError(f"{self.samples.shape=} should be a vector")

        if not (self.samples.dtype.kind in "iuf"):
            raise ValueError(f"{self.samples.dtype=} should be real numeric")

        sample_finiteness = np.isfinite(self.samples)

        if not sample_finiteness.all():
            sample_index = next(index for index in range(self.samples.size) if not (sample_finiteness[index]))
            sample = self.samples[sample_index]

            raise ValueError(f"{sample=} at {sample_index=} should be finite")


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationResult:
    count: int

    def __post_init__(self) -> None:
        actual_count = self.count

        require_type(actual_count, int)

        if not (0 < self.count):
            raise ValueError(f"{self.count=} should be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class PredictionResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        if not np.isfinite(self.total):
            raise ValueError(f"{self.total=} should be finite")

        actual_count = self.count

        require_type(actual_count, int)

        if not (0 < self.count):
            raise ValueError(f"{self.count=} should be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class SumResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        if not np.isfinite(self.total):
            raise ValueError(f"{self.total=} should be finite")

        actual_count = self.count

        require_type(actual_count, int)

        if not (0 < self.count):
            raise ValueError(f"{self.count=} should be positive")


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

        if not ((self.samples.ndim == 1) and (self.samples.size > 0)):
            raise ValueError(f"{self.samples.shape=} should be a nonempty vector")

        if not (self.samples.dtype.kind in "iuf"):
            raise ValueError(f"{self.samples.dtype=} should be real numeric")

        self.samples = np.asarray(a=self.samples, dtype=float)

        sample_finiteness = np.isfinite(self.samples)

        if not sample_finiteness.all():
            sample_index = next(index for index in range(self.samples.size) if not (sample_finiteness[index]))
            sample = self.samples[sample_index]

            raise ValueError(f"{sample=} at {sample_index=} should be finite")

    @property
    def sample_count(self) -> int:
        if not (self.samples is not None):
            raise RuntimeError(f"{self.samples=} should be set")

        return self.samples.size

    def consume(self, *, analysis: WeightedSumAnalysis) -> np.ndarray:
        if not (self.analysis is analysis):
            raise RuntimeError(f"{id(self.analysis)=} ≠ {id(analysis)=}")

        if not (analysis.accepted_preparation is self):
            raise RuntimeError(f"{id(analysis.accepted_preparation)=} ≠ {id(self)=}")

        if not (self.samples is not None):
            raise RuntimeError(f"{self.samples=} should be set")

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
        raise RuntimeError(f"{id(model)=} already has loaded coefficients")

    # Validate the copy the model will retain; assembly still owns its array.
    coefficient_weights = model.coefficients.weights.copy()

    if not ((coefficient_weights.ndim == 1) and (coefficient_weights.size > 0)):
        raise ValueError(f"{coefficient_weights.shape=} should be a nonempty vector")

    if not (coefficient_weights.dtype.kind in "iuf"):
        raise ValueError(f"{coefficient_weights.dtype=} should be real numeric")

    coefficient_weights = np.asarray(a=coefficient_weights, dtype=float)

    coefficient_finiteness = np.isfinite(coefficient_weights)

    if not coefficient_finiteness.all():
        coefficient_index = next(index for index in range(coefficient_weights.size) if not (coefficient_finiteness[index]))
        coefficient_weight = coefficient_weights[coefficient_index]

        raise ValueError(f"{coefficient_weight=} at {coefficient_index=} should be finite")

    model.loaded_coefficients = coefficient_weights


def dispose_model(model: WeightedSumModel) -> None:
    model.loaded_coefficients = None


def create_analysis(model: WeightedSumModel) -> WeightedSumAnalysis:
    return WeightedSumAnalysis(model=model)


def initialize_analysis(analysis: WeightedSumAnalysis) -> None:
    if not (analysis.is_open is False):
        raise RuntimeError(f"{analysis.is_open=} should be false")

    if not (analysis.model.loaded_coefficients is not None):
        raise RuntimeError(f"{analysis.model.loaded_coefficients=} should be set")

    analysis.is_open = True


def prepare(
    analysis: WeightedSumAnalysis,
    candidate: PreparationInput,
) -> PreparedSamples:
    """Reject one candidate without discarding the analysis itself."""

    require_type(candidate, PreparationInput)

    if not (analysis.is_open):
        raise RuntimeError(f"{analysis.is_open=} should be true")

    if not (candidate.samples.size > 0):
        raise ScientificInputRejected(
            f"{candidate.samples.size=} should be positive",
            code=ScientificInputRejected.Code.EMPTY_INPUT,
        )

    if not (analysis.accepted_preparation is None):
        raise ScientificInputRejected(
            f"{id(analysis.accepted_preparation)=} should be consumed or disposed before another preparation",
            code=ScientificInputRejected.Code.ALREADY_PREPARED,
        )

    loaded_coefficients = analysis.model.loaded_coefficients

    if not (loaded_coefficients is not None):
        raise RuntimeError(f"{loaded_coefficients=} should be set")

    actual_sample_count = candidate.samples.size
    expected_sample_count = loaded_coefficients.size

    if not (actual_sample_count == expected_sample_count):
        raise ScientificInputRejected(
            f"{actual_sample_count=} should equal {expected_sample_count=}",
            code=ScientificInputRejected.Code.WRONG_COUNT,
        )

    prepared_samples = PreparedSamples(analysis=analysis, samples=candidate.samples)
    analysis.accepted_preparation = prepared_samples

    return prepared_samples


def calculate(
    analysis: WeightedSumAnalysis,
    state: CalculationState,
) -> PredictionResult:
    require_type(state, CalculationState)

    if not (analysis.is_open):
        raise RuntimeError(f"{analysis.is_open=} should be true")

    loaded_coefficients = analysis.model.loaded_coefficients

    if not (loaded_coefficients is not None):
        raise RuntimeError(f"{loaded_coefficients=} should be set")

    prepared_samples = state.prepared.consume(analysis=analysis)

    # State is consumed before numerical work. A later failure does not make the
    # old state reference usable again.
    weighted_sum = float(np.dot(a=loaded_coefficients, b=prepared_samples))

    prediction_total = analysis.model.settings.offset + analysis.model.settings.scale * weighted_sum

    if not np.isfinite(prediction_total):
        raise ArithmeticError(f"{prediction_total=} should be finite")

    return PredictionResult(total=prediction_total, count=prepared_samples.size)


def sum_values(summation: SumInput) -> SumResult:
    require_type(summation, SumInput)

    if not (summation.samples.size > 0):
        raise ScientificInputRejected(
            f"{summation.samples.size=} should be positive",
            code=ScientificInputRejected.Code.EMPTY_INPUT,
        )

    # Floating accumulation preserves the example's real-valued calculation;
    # an integer accumulator can wrap before a later finiteness check sees it.
    sum_total = float(np.sum(a=summation.samples, dtype=float))

    if not np.isfinite(sum_total):
        raise ArithmeticError(f"{sum_total=} should be finite")

    return SumResult(total=sum_total, count=summation.samples.size)


def dispose_prepared(prepared: PreparedSamples) -> None:
    """Release retained values without changing the analysis lifetime."""
    prepared.dispose()


def dispose_analysis(analysis: WeightedSumAnalysis) -> None:
    accepted_preparation = analysis.accepted_preparation

    if accepted_preparation is not None:
        dispose_prepared(accepted_preparation)

    analysis.is_open = False
