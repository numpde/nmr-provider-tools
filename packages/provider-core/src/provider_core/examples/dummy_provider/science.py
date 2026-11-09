"""Fake provider-owned science with explicit portable input and output types.

This module imports no provider-core code. The same types and functions are used
directly and through the contribution adapter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _as_finite_float(value: object, *, label: str) -> float:
    if not _finite_number(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


class InputRejected(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    scale: float
    offset: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "scale", _as_finite_float(self.scale, label="Scale"))
        object.__setattr__(self, "offset", _as_finite_float(self.offset, label="Offset"))


@dataclass(frozen=True, slots=True, kw_only=True)
class ValuesInput:
    """Portable provider input admitted from the declared payload format."""

    values: tuple[float, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class EmptyInput:
    """Portable input for a call whose scientific arguments are retained state."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationResult:
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PredictionResult:
    total: float
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SumResult:
    total: float
    count: int


@dataclass(slots=True, kw_only=True)
class Model:
    settings: Settings
    coefficients: tuple[float, ...]
    loaded: tuple[float, ...] | None = None


@dataclass(slots=True, kw_only=True)
class AnalysisState:
    model: Model
    open: bool = False
    preparations: int = 0
    accepted: PreparedInput | None = None


@dataclass(slots=True, kw_only=True)
class PreparedInput:
    analysis: AnalysisState
    values: tuple[float, ...]
    usable: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class CalculationState:
    """Provider-owned typed view of the retained state required by calculate."""

    prepared: PreparedInput


@dataclass(frozen=True, slots=True, kw_only=True)
class NoState:
    """Provider-owned state struct for calls that retain no live values."""


def require_configuration(settings: Settings, coefficients: tuple[float, ...]) -> None:
    if type(settings) is not Settings:
        raise TypeError("The model requires explicit Settings")
    if not (
        type(coefficients) is tuple
        and coefficients
        and all(_finite_number(value) for value in coefficients)
    ):
        raise ValueError("Coefficient assets must be a nonempty tuple of finite numbers")


def create_model(settings: Settings, coefficients: tuple[float, ...]) -> Model:
    """Record immutable configuration; load_model performs resource acquisition."""
    require_configuration(settings, coefficients)
    return Model(
        settings=settings,
        coefficients=tuple(float(value) for value in coefficients),
    )


def load_model(model: Model) -> None:
    if model.loaded is not None:
        raise RuntimeError("The model is already loaded")
    model.loaded = model.coefficients


def dispose_model(model: Model) -> None:
    model.loaded = None


def create_analysis(model: Model) -> AnalysisState:
    return AnalysisState(model=model)


def initialize_analysis(analysis: AnalysisState) -> None:
    if analysis.model.loaded is None:
        raise RuntimeError("An analysis requires a loaded model")
    analysis.open = True


def require_values(candidate: ValuesInput) -> None:
    if type(candidate) is not ValuesInput:
        raise TypeError("Scientific values must be supplied as ValuesInput")
    if not candidate.values:
        raise InputRejected("empty_input", "Supply at least one value")
    if not all(_finite_number(value) for value in candidate.values):
        raise InputRejected("invalid_value", "Every input value must be a finite number")


def prepare(analysis: AnalysisState, candidate: ValuesInput) -> PreparedInput:
    """Reject a candidate without losing the analysis state."""
    if not analysis.open:
        raise RuntimeError("The analysis is closed")
    analysis.preparations += 1
    require_values(candidate)
    if analysis.accepted is not None:
        raise InputRejected(
            "already_prepared",
            "Calculate or discard the accepted input before preparing another",
        )
    coefficients = analysis.model.loaded
    if coefficients is None:
        raise RuntimeError("The model is not loaded")
    if len(candidate.values) != len(coefficients):
        raise InputRejected("wrong_count", "Input and coefficient counts must agree")
    prepared = PreparedInput(
        analysis=analysis,
        values=candidate.values,
    )
    analysis.accepted = prepared
    return prepared


def calculate(
    analysis: AnalysisState,
    request: EmptyInput,
    state: CalculationState,
) -> PredictionResult:
    if type(request) is not EmptyInput:
        raise TypeError("Calculation requires EmptyInput")
    if type(state) is not CalculationState:
        raise TypeError("Calculation requires CalculationState")
    prepared = state.prepared
    if not analysis.open or analysis.accepted is not prepared or not prepared.usable:
        raise RuntimeError("Calculation requires this analysis's accepted input")
    prepared.usable = False
    analysis.accepted = None
    coefficients = analysis.model.loaded
    if coefficients is None:
        raise RuntimeError("The model is not loaded")
    weighted = sum(
        coefficient * value
        for coefficient, value in zip(coefficients, prepared.values, strict=True)
    )
    total = analysis.model.settings.offset + analysis.model.settings.scale * weighted
    if not math.isfinite(total):
        raise ArithmeticError("The weighted sum is not finite")
    return PredictionResult(
        total=float(total),
        count=len(prepared.values),
    )


def sum_values(candidate: ValuesInput) -> SumResult:
    require_values(candidate)
    total = sum(candidate.values)
    if not math.isfinite(total):
        raise ArithmeticError("The sum is not finite")
    return SumResult(
        total=float(total),
        count=len(candidate.values),
    )


def dispose_prepared(prepared: PreparedInput) -> None:
    """Consumption and physical disposal remain separate events."""
    if prepared.analysis.accepted is prepared:
        prepared.analysis.accepted = None
    prepared.usable = False
    prepared.values = ()


def dispose_analysis(analysis: AnalysisState) -> None:
    if analysis.accepted is not None:
        dispose_prepared(analysis.accepted)
    analysis.open = False
