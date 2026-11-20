"""Dummy provider-owned science for the shared contribution example.

The module uses the SDK's scientific rejection and type requirement contracts.
Portable records are immutable; live model, analysis, and prepared state remain
explicit and provider-owned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import auto

from provider_core.contribution_errors import ErrorCode, ScientificRejection
from provider_core.tools import require_finite_number
from provider_core.type_requirements import require_instance, require_type


def _finite_values(numeric_components: object) -> tuple[float, ...]:
    require_type(numeric_components, tuple)

    # repo-check: allow-tuple-generator -- Frozen coefficient/input records must keep their numeric contents stable after model loading or preparation
    return tuple(
        float(require_finite_number(numeric_component))
        for numeric_component in numeric_components
    )


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
        object.__setattr__(
            self,
            "scale",
            float(require_finite_number(self.scale)),
        )

        object.__setattr__(
            self,
            "offset",
            float(require_finite_number(self.offset)),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CoefficientSet:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        normalized_values = _finite_values(self.values)

        if not (normalized_values):
            raise ValueError("coefficient values must not be empty")

        object.__setattr__(self, "values", normalized_values)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationInput:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "values",
            _finite_values(self.values),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SumInput:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "values",
            _finite_values(self.values),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationResult:
    count: int

    def __post_init__(self) -> None:
        require_type(self.count, int, error_factory=ValueError)

        if not (0 < self.count):
            raise ValueError(f"{self.count=} should be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class PredictionResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total",
            float(require_finite_number(self.total)),
        )

        require_type(self.count, int, error_factory=ValueError)

        if not (0 < self.count):
            raise ValueError(f"{self.count=} should be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class SumResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total",
            float(require_finite_number(self.total)),
        )

        require_type(self.count, int, error_factory=ValueError)

        if not (0 < self.count):
            raise ValueError(f"{self.count=} should be positive")


@dataclass(slots=True, kw_only=True)
class WeightedSumModel:
    settings: WeightedSumSettings
    coefficients: CoefficientSet

    loaded_coefficients: tuple[float, ...] | None = field(
        default=None,
        init=False,
        repr=False,
    )


@dataclass(slots=True, kw_only=True)
class WeightedSumAnalysis:
    model: WeightedSumModel
    is_open: bool = field(default=False, init=False)

    accepted_preparation: PreparedValues | None = field(
        default=None,
        init=False,
        repr=False,
    )


@dataclass(slots=True, kw_only=True)
class PreparedValues:
    """Worker-local values owned by exactly one scientific analysis."""

    analysis: WeightedSumAnalysis
    values: tuple[float, ...] | None = field(repr=False)

    def __post_init__(self) -> None:
        if not (self.values):
            raise ValueError("prepared values must not be empty")

        self.values = _finite_values(self.values)

    @property
    def value_count(self) -> int:
        if self.values is None:
            raise RuntimeError("The prepared values have already been consumed or disposed")

        return len(self.values)

    def consume(self, *, analysis: WeightedSumAnalysis) -> tuple[float, ...]:
        if not (self.analysis is analysis):
            raise RuntimeError("The prepared values belong to another analysis")

        if not (analysis.accepted_preparation is self):
            raise RuntimeError("The prepared values are not this analysis's accepted preparation")

        if self.values is None:
            raise RuntimeError("The prepared values have already been consumed or disposed")

        prepared_values = self.values
        analysis.accepted_preparation = None
        self.values = None

        return prepared_values

    def dispose(self) -> None:
        if self.analysis.accepted_preparation is self:
            self.analysis.accepted_preparation = None

        self.values = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CalculationState:
    prepared: PreparedValues


def create_model(
    *,
    settings: WeightedSumSettings,
    coefficients: CoefficientSet,
) -> WeightedSumModel:
    return WeightedSumModel(
        settings=settings,
        coefficients=coefficients,
    )


def load_model(model: WeightedSumModel) -> None:
    if not (model.loaded_coefficients is None):
        raise RuntimeError("The model coefficients are already loaded")

    model.loaded_coefficients = model.coefficients.values


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
) -> PreparedValues:
    """Reject one candidate without discarding the analysis itself."""

    if not (analysis.is_open):
        raise RuntimeError("Cannot prepare input for a closed analysis")

    if not (candidate.values):
        raise ScientificInputRejected(
            "Preparation input must contain at least one value",
            code=ScientificInputRejected.Code.EMPTY_INPUT,
        )

    if not (analysis.accepted_preparation is None):
        raise ScientificInputRejected(
            "Calculate or discard the accepted preparation before preparing another",
            code=ScientificInputRejected.Code.ALREADY_PREPARED,
        )

    loaded_coefficients = analysis.model.loaded_coefficients

    if loaded_coefficients is None:
        raise RuntimeError("Cannot prepare input because model coefficients are not loaded")

    actual_value_count = len(candidate.values)
    expected_value_count = len(loaded_coefficients)

    if actual_value_count != expected_value_count:
        raise ScientificInputRejected(
            f"{actual_value_count = }, {expected_value_count = }",
            code=ScientificInputRejected.Code.WRONG_COUNT,
        )

    prepared = PreparedValues(
        analysis=analysis,
        values=candidate.values,
    )

    analysis.accepted_preparation = prepared

    return prepared


def calculate(
    analysis: WeightedSumAnalysis,
    state: CalculationState,
) -> PredictionResult:
    if not (analysis.is_open):
        raise RuntimeError("Cannot calculate for a closed analysis")

    loaded_coefficients = analysis.model.loaded_coefficients

    if loaded_coefficients is None:
        raise RuntimeError("Cannot calculate because model coefficients are not loaded")

    prepared_values = state.prepared.consume(analysis=analysis)

    # State is consumed before numerical work. A later failure does not make the
    # old state reference usable again.
    weighted_sum = math.fsum(
        (coefficient * prepared_value)
        for (coefficient, prepared_value) in zip(
            loaded_coefficients,
            prepared_values,
            strict=True,
        )
    )

    prediction_total = (
        analysis.model.settings.offset
        + analysis.model.settings.scale * weighted_sum
    )

    if not math.isfinite(prediction_total):
        raise ArithmeticError(f"{prediction_total=} should be finite")

    return PredictionResult(
        total=prediction_total,
        count=len(prepared_values),
    )


def sum_values(summation: SumInput) -> SumResult:
    if not (summation.values):
        raise ScientificInputRejected(
            "Sum input must contain at least one value",
            code=ScientificInputRejected.Code.EMPTY_INPUT,
        )

    sum_total = math.fsum(summation.values)

    if not math.isfinite(sum_total):
        raise ArithmeticError(f"{sum_total=} should be finite")

    return SumResult(
        total=sum_total,
        count=len(summation.values),
    )


def dispose_prepared(prepared: PreparedValues) -> None:
    """Release retained values without changing the analysis lifetime."""
    prepared.dispose()


def dispose_analysis(analysis: WeightedSumAnalysis) -> None:
    accepted_preparation = analysis.accepted_preparation

    if accepted_preparation is not None:
        dispose_prepared(accepted_preparation)

    analysis.is_open = False
