"""Portable spectra, explicit settings, baseline correction and peak detection."""

from __future__ import annotations

import math
from dataclasses import dataclass

from provider_core.contribution_errors import ScientificRejection
from provider_core.tools import require_finite_number


def _finite_samples(raw_samples: object, *, series_name: str) -> tuple[float, ...]:
    if not (type(raw_samples) is (expected_type := tuple)):
        raise TypeError(f"{series_name=}; {type(raw_samples)=} ≠ {expected_type=}")

    samples = []

    for raw_sample in raw_samples:
        samples.append(float(require_finite_number(raw_sample)))

    return tuple(samples)


@dataclass(frozen=True, slots=True, kw_only=True)
class Spectrum:
    positions: tuple[float, ...]
    intensities: tuple[float, ...]

    def __post_init__(self) -> None:
        positions = _finite_samples(self.positions, series_name="positions")
        intensities = _finite_samples(self.intensities, series_name="intensities")
        position_count = len(positions)
        intensity_count = len(intensities)

        if (not positions) or (position_count != intensity_count):
            raise ValueError(
                f"A spectrum requires nonempty, equally sized positions and intensities; "
                f"received {position_count=} and {intensity_count=}"
            )

        for position_index in range(1, len(positions)):
            previous_position = positions[position_index - 1]
            current_position = positions[position_index]

            if not (previous_position < current_position):
                raise ValueError(
                    f"Spectrum positions must strictly increase; "
                    f"{position_index=}; {previous_position=}; {current_position=}"
                )

        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "intensities", intensities)


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineSettings:
    baseline: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "baseline", float(require_finite_number(self.baseline)))


@dataclass(frozen=True, slots=True, kw_only=True)
class PeakSettings:
    minimum_height: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_height",
            float(require_finite_number(self.minimum_height)),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Peak:
    position: float
    height: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "position", float(require_finite_number(self.position)))
        object.__setattr__(self, "height", float(require_finite_number(self.height)))


@dataclass(frozen=True, slots=True, kw_only=True)
class PeakReport:
    peaks: tuple[Peak, ...]

    def __post_init__(self) -> None:
        if not (type((actual_peaks := self.peaks)) is (expected_type := tuple)):
            raise TypeError(f"{type(actual_peaks)=} ≠ {expected_type=}")

        for peak in self.peaks:
            if not (type(peak) is (expected_type := Peak)):
                raise TypeError(f"{type(peak)=} ≠ {expected_type=}")


def subtract_baseline(spectrum: Spectrum, settings: BaselineSettings) -> Spectrum:
    """Subtract the configured constant without clipping negative intensities."""
    corrected = []

    for intensity in spectrum.intensities:
        corrected_intensity = intensity - settings.baseline

        if not math.isfinite(corrected_intensity):
            raise ArithmeticError(f"Baseline subtraction produced a nonfinite {corrected_intensity=}")

        corrected.append(corrected_intensity)

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
            (height >= settings.minimum_height) and
            (height > spectrum.intensities[index - 1]) and
            (height > spectrum.intensities[index + 1])
        ):
            peaks.append(Peak(position=spectrum.positions[index], height=height))

    return PeakReport(peaks=tuple(peaks))
