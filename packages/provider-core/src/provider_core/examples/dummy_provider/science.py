"""Dummy provider-owned science for the shared contribution example.

This module has no dependency on provider-core. Portable records are immutable;
live model, analysis, and prepared state remain explicit and provider-owned.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


_REJECTION_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _finite_float(value: object, *, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _finite_values(values: object, *, label: str) -> tuple[float, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be a tuple")
    return tuple(
        _finite_float(value, label=f"{label}[{index}]")
        for (index, value) in enumerate(values)
    )


def _require_exact_type[T](
    value: object,
    expected_type: type[T],
    *,
    label: str,
) -> T:
    if type(value) is not expected_type:
        raise TypeError(f"{label} must be {expected_type.__qualname__}")
    return value


class ScientificInputRejected(ValueError):
    """A stable scientific refusal after portable input has been admitted."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        if type(code) is not str or _REJECTION_CODE.fullmatch(code) is None:
            raise ValueError("code must match [a-z][a-z0-9_]{0,63}")
        if not (type(message) is str and message):
            raise ValueError("message must be a nonempty string")
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedSumSettings:
    scale: float
    offset: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scale",
            _finite_float(self.scale, label="scale"),
        )
        object.__setattr__(
            self,
            "offset",
            _finite_float(self.offset, label="offset"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlainSumConfiguration:
    """Immutable marker for the stateless implementation revision."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CoefficientSet:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        normalized_values = _finite_values(
            self.values,
            label="coefficient values",
        )
        if not normalized_values:
            raise ValueError("coefficient values must not be empty")
        object.__setattr__(self, "values", normalized_values)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationInput:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "values",
            _finite_values(self.values, label="prepare input values"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CalculationInput:
    """No portable arguments; calculation uses retained prepared state."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SumInput:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "values",
            _finite_values(self.values, label="sum input values"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationResult:
    count: int

    def __post_init__(self) -> None:
        if not (type(self.count) is int and 0 < self.count):
            raise ValueError("count must be a positive integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class PredictionResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total",
            _finite_float(self.total, label="prediction total"),
        )
        if not (type(self.count) is int and 0 < self.count):
            raise ValueError("count must be a positive integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class SumResult:
    total: float
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total",
            _finite_float(self.total, label="sum total"),
        )
        if not (type(self.count) is int and 0 < self.count):
            raise ValueError("count must be a positive integer")


@dataclass(slots=True, kw_only=True)
class WeightedSumModel:
    settings: WeightedSumSettings
    coefficients: CoefficientSet
    loaded_coefficients: tuple[float, ...] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        _require_exact_type(self.settings, WeightedSumSettings, label="settings")
        _require_exact_type(self.coefficients, CoefficientSet, label="coefficients")


@dataclass(slots=True, kw_only=True)
class WeightedSumAnalysis:
    model: WeightedSumModel
    is_open: bool = field(default=False, init=False)
    preparation_attempts: int = field(default=0, init=False)
    accepted_preparation: PreparedValues | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        _require_exact_type(self.model, WeightedSumModel, label="model")


@dataclass(slots=True, kw_only=True)
class PreparedValues:
    """Worker-local values owned by exactly one scientific analysis."""

    analysis: WeightedSumAnalysis
    values: tuple[float, ...] | None = field(repr=False)

    def __post_init__(self) -> None:
        _require_exact_type(self.analysis, WeightedSumAnalysis, label="analysis")
        if not self.values:
            raise ValueError("prepared values must not be empty")
        self.values = _finite_values(self.values, label="prepared values")

    @property
    def value_count(self) -> int:
        if self.values is None:
            raise RuntimeError(
                "The prepared values have already been consumed or disposed"
            )
        return len(self.values)

    def consume(self, *, analysis: WeightedSumAnalysis) -> tuple[float, ...]:
        if self.analysis is not analysis:
            raise RuntimeError("The prepared values belong to another analysis")
        if analysis.accepted_preparation is not self:
            raise RuntimeError(
                "The prepared values are not this analysis's accepted preparation"
            )
        if self.values is None:
            raise RuntimeError(
                "The prepared values have already been consumed or disposed"
            )

        prepared_values = self.values
        analysis.accepted_preparation = None
        self.values = None
        return prepared_values

    def dispose(self) -> None:
        if self.analysis.accepted_preparation is self:
            self.analysis.accepted_preparation = None
        self.values = None


@dataclass(frozen=True, slots=True, kw_only=True)
class NoLiveState:
    """Typed marker for a call with no retained scientific state."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CalculationState:
    prepared: PreparedValues

    def __post_init__(self) -> None:
        _require_exact_type(self.prepared, PreparedValues, label="prepared")


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
    _require_exact_type(model, WeightedSumModel, label="model")

    if model.loaded_coefficients is not None:
        raise RuntimeError("The model coefficients are already loaded")
    model.loaded_coefficients = model.coefficients.values


def dispose_model(model: WeightedSumModel) -> None:
    _require_exact_type(model, WeightedSumModel, label="model")
    model.loaded_coefficients = None


def create_analysis(model: WeightedSumModel) -> WeightedSumAnalysis:
    return WeightedSumAnalysis(model=model)


def initialize_analysis(analysis: WeightedSumAnalysis) -> None:
    _require_exact_type(analysis, WeightedSumAnalysis, label="analysis")

    if analysis.is_open:
        raise RuntimeError("The analysis is already initialized")
    if analysis.model.loaded_coefficients is None:
        raise RuntimeError(
            "Cannot initialize an analysis before loading model coefficients"
        )
    analysis.is_open = True


def prepare(
    analysis: WeightedSumAnalysis,
    candidate: PreparationInput,
) -> PreparedValues:
    """Reject one candidate without discarding the analysis itself."""
    _require_exact_type(analysis, WeightedSumAnalysis, label="analysis")
    _require_exact_type(candidate, PreparationInput, label="candidate")

    if not analysis.is_open:
        raise RuntimeError("Cannot prepare input for a closed analysis")

    analysis.preparation_attempts += 1

    if not candidate.values:
        raise ScientificInputRejected(
            "empty_input",
            "Preparation input must contain at least one value",
        )
    if analysis.accepted_preparation is not None:
        raise ScientificInputRejected(
            "already_prepared",
            "Calculate or discard the accepted preparation before preparing another",
        )

    loaded_coefficients = analysis.model.loaded_coefficients
    if loaded_coefficients is None:
        raise RuntimeError(
            "Cannot prepare input because model coefficients are not loaded"
        )

    actual_value_count = len(candidate.values)
    expected_value_count = len(loaded_coefficients)

    if actual_value_count != expected_value_count:
        raise ScientificInputRejected(
            "wrong_count",
            f"{actual_value_count = }, {expected_value_count = }",
        )

    prepared = PreparedValues(
        analysis=analysis,
        values=candidate.values,
    )
    analysis.accepted_preparation = prepared
    return prepared


def calculate(
    analysis: WeightedSumAnalysis,
    calculation: CalculationInput,
    state: CalculationState,
) -> PredictionResult:
    _require_exact_type(analysis, WeightedSumAnalysis, label="analysis")
    _require_exact_type(calculation, CalculationInput, label="calculation")
    _require_exact_type(state, CalculationState, label="state")

    if not analysis.is_open:
        raise RuntimeError("Cannot calculate for a closed analysis")

    loaded_coefficients = analysis.model.loaded_coefficients
    if loaded_coefficients is None:
        raise RuntimeError("Cannot calculate because model coefficients are not loaded")

    prepared = state.prepared
    prepared_values = prepared.consume(analysis=analysis)

    # consume() ran before numerical work. A later failure does not make the old
    # state reference usable again.
    weighted_sum = math.fsum(
        coefficient * value
        for (coefficient, value) in zip(
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
        raise ArithmeticError("The weighted prediction total is not finite")

    return PredictionResult(
        total=prediction_total,
        count=len(prepared_values),
    )


def sum_values(summation: SumInput) -> SumResult:
    _require_exact_type(summation, SumInput, label="summation")

    if not summation.values:
        raise ScientificInputRejected(
            "empty_input",
            "Sum input must contain at least one value",
        )

    total = math.fsum(summation.values)
    if not math.isfinite(total):
        raise ArithmeticError("The sum total is not finite")

    return SumResult(
        total=total,
        count=len(summation.values),
    )


def dispose_prepared(prepared: PreparedValues) -> None:
    """Release retained values without changing the analysis lifetime."""
    _require_exact_type(prepared, PreparedValues, label="prepared")
    prepared.dispose()


def dispose_analysis(analysis: WeightedSumAnalysis) -> None:
    _require_exact_type(analysis, WeightedSumAnalysis, label="analysis")

    accepted_preparation = analysis.accepted_preparation
    if accepted_preparation is not None:
        dispose_prepared(accepted_preparation)
    analysis.is_open = False
