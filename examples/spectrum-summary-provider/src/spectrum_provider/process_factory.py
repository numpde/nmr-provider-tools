"""Construct each independent scientific stage inside its installed worker."""

from collections.abc import Mapping

from .binding import BaselineCorrector, PeakDetector
from .science import BaselineSettings, PeakSettings


def construct_baseline(*, startup: Mapping) -> BaselineCorrector:
    return BaselineCorrector(configuration=BaselineSettings(**startup))


def construct_peaks(*, startup: Mapping) -> PeakDetector:
    return PeakDetector(configuration=PeakSettings(**startup))
