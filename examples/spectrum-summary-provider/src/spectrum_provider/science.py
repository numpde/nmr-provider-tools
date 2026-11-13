"""Portable spectra, explicit settings, baseline correction and peak detection."""

from __future__ import annotations

import math
from dataclasses import dataclass

from provider_core.contribution_errors import ScientificRejection


def _finite_number(value: object, *, label: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{label} must be a finite number; booleans are not measurements")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


def _finite_samples(values: object, *, label: str) -> tuple[float, ...]:
    if type(values) is not tuple:
        raise TypeError(f"Spectrum {label} must be an immutable tuple")
    samples = []
    for index, value in enumerate(values):
        samples.append(_finite_number(value, label=f"Spectrum {label}[{index}]"))
    return tuple(samples)


@dataclass(frozen=True, slots=True, kw_only=True)
class Spectrum:
    positions: tuple[float, ...]
    intensities: tuple[float, ...]

    def __post_init__(self) -> None:
        positions = _finite_samples(self.positions, label="positions")
        intensities = _finite_samples(self.intensities, label="intensities")
        if not positions or len(positions) != len(intensities):
            raise ValueError(
                "A spectrum requires nonempty, equally sized positions and intensities; "
                f"received {len(positions)} positions and {len(intensities)} intensities"
            )
        for index in range(1, len(positions)):
            if not (positions[index - 1] < positions[index]):
                raise ValueError(
                    "Spectrum positions must strictly increase; "
                    f"position {index} does not exceed position {index - 1}"
                )
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "intensities", intensities)


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineSettings:
    baseline: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "baseline", _finite_number(self.baseline, label="Baseline"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PeakSettings:
    minimum_height: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_height",
            _finite_number(self.minimum_height, label="Minimum peak height"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Peak:
    position: float
    height: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "position", _finite_number(self.position, label="Peak position"))
        object.__setattr__(self, "height", _finite_number(self.height, label="Peak height"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PeakReport:
    peaks: tuple[Peak, ...]

    def __post_init__(self) -> None:
        if type(self.peaks) is not tuple or any(type(peak) is not Peak for peak in self.peaks):
            raise TypeError("A peak report requires an immutable tuple of Peak records")


def subtract_baseline(spectrum: Spectrum, settings: BaselineSettings) -> Spectrum:
    """Subtract the configured constant without clipping negative intensities."""
    corrected = []
    for intensity in spectrum.intensities:
        value = intensity - settings.baseline
        if not math.isfinite(value):
            raise ArithmeticError("Baseline subtraction produced a nonfinite intensity")
        corrected.append(value)
    return Spectrum(positions=spectrum.positions, intensities=tuple(corrected))


def detect_peaks(spectrum: Spectrum, settings: PeakSettings) -> PeakReport:
    """Find interior strict maxima at or above the configured minimum height."""
    if len(spectrum.positions) < 3:
        raise ScientificRejection(
            "too_few_samples",
            "Peak detection requires at least three samples to compare an interior point with both neighbours",
        )
    peaks = []
    for index in range(1, len(spectrum.positions) - 1):
        height = spectrum.intensities[index]
        if (
            height >= settings.minimum_height
            and height > spectrum.intensities[index - 1]
            and height > spectrum.intensities[index + 1]
        ):
            peaks.append(Peak(position=spectrum.positions[index], height=height))
    return PeakReport(peaks=tuple(peaks))
