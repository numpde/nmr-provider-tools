"""Fake science: a configured weighted sum and a stateless ordinary sum.

This file deliberately uses no provider-core types. The same functions can be
called directly or through binding.py.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


class InputRejected(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class Settings:
    scale: float
    offset: float

    def __post_init__(self):
        if not (_finite(self.scale) and _finite(self.offset)):
            raise ValueError("Scale and offset must be finite numbers")


def require_configuration(settings: Settings, coefficients: tuple[float, ...]) -> None:
    if type(settings) is not Settings:
        raise TypeError("The model requires explicit Settings")
    if not (type(coefficients) is tuple and coefficients and all(_finite(value) for value in coefficients)):
        raise ValueError("Coefficient assets must be a nonempty tuple of finite numbers")


class Model:
    def __init__(self, settings: Settings, coefficients: tuple[float, ...]):
        require_configuration(settings, coefficients)
        self.settings, self.coefficients = settings, coefficients
        self.loaded = None


def create_model(settings: Settings, coefficients: tuple[float, ...]) -> Model:
    """Record immutable configuration; load_model does the actual loading."""
    return Model(settings, coefficients)


def load_model(model: Model) -> None:
    if model.loaded is not None:
        raise RuntimeError("The model is already loaded")
    # This list stands in for a model allocation. It remains reachable on failure.
    model.loaded = list(model.coefficients)


def dispose_model(model: Model) -> None:
    model.loaded = None


@dataclass(slots=True)
class PreparedInput:
    analysis: AnalysisState
    values: tuple[float, ...]
    usable: bool


class AnalysisState:
    def __init__(self, model: Model):
        self.model = model
        self.open = False
        self.preparations = 0
        self.accepted = None


def create_analysis(model: Model) -> AnalysisState:
    return AnalysisState(model)


def initialize_analysis(analysis: AnalysisState) -> None:
    if analysis.model.loaded is None:
        raise RuntimeError("An analysis requires a loaded model")
    analysis.open = True


def prepare(analysis: AnalysisState, values: tuple[float, ...]) -> PreparedInput:
    """Reject a candidate without losing the scientific analysis state."""
    if not analysis.open:
        raise RuntimeError("The analysis is closed")
    analysis.preparations += 1
    require_values(values)
    if analysis.accepted is not None:
        raise InputRejected("already_prepared", "Calculate or discard the accepted input before preparing another")
    if len(values) != len(analysis.model.loaded):
        raise InputRejected("wrong_count", "Input and coefficient counts must agree")
    prepared = PreparedInput(analysis, values, True)
    analysis.accepted = prepared
    return prepared


def calculate(analysis: AnalysisState, prepared: PreparedInput) -> dict:
    if not analysis.open or analysis.accepted is not prepared or not prepared.usable:
        raise RuntimeError("Calculation requires this analysis's accepted input")
    prepared.usable = False
    analysis.accepted = None
    model = analysis.model
    weighted = sum(coefficient * value for coefficient, value in zip(model.loaded, prepared.values, strict=True))
    total = model.settings.offset + model.settings.scale * weighted
    if not math.isfinite(total):
        raise ArithmeticError("The weighted sum is not finite")
    return {"total": total, "count": len(prepared.values)}


def dispose_prepared(prepared: PreparedInput) -> None:
    """Consumption and release of the retained values are separate events."""
    if prepared.analysis.accepted is prepared:
        prepared.analysis.accepted = None
    prepared.usable = False
    prepared.values = ()


def dispose_analysis(analysis: AnalysisState) -> None:
    # Also reach partial preparation that failed before returning to the host.
    if analysis.accepted is not None:
        dispose_prepared(analysis.accepted)
    analysis.open = False


def require_values(values: tuple[float, ...]) -> None:
    if not (type(values) is tuple and values):
        raise InputRejected("empty_input", "Supply at least one value")
    if not all(_finite(value) for value in values):
        raise InputRejected("invalid_value", "Every input value must be a finite number")


def sum_values(values: tuple[float, ...]) -> dict:
    require_values(values)
    total = sum(values)
    if not math.isfinite(total):
        raise ArithmeticError("The sum is not finite")
    return {"total": total, "count": len(values)}
