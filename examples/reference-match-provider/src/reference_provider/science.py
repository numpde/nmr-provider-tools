"""Cosine matching of nonnegative channel vectors using immutable postings."""

from __future__ import annotations

from dataclasses import dataclass
import math
import reprlib

from provider_core.contribution_errors import ScientificRejection
from provider_core.tools import require_nonnegative_number
from provider_core.type_requirements import require_type


def _finite(numeric_value: object) -> float:
    return float(require_nonnegative_number(numeric_value))


@dataclass(frozen=True, slots=True, kw_only=True)
class VectorInput:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        actual_values = self.values

        require_type(actual_values, tuple)

        if not (self.values):
            raise ValueError("A matching vector requires at least one channel value")

        finite_channels = []

        for channel in self.values:
            finite_channels.append(_finite(channel))

        object.__setattr__(self, "values", tuple(finite_channels))


@dataclass(frozen=True, slots=True, kw_only=True)
class Reference:
    label: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        actual_label = self.label

        require_type(actual_label, str)

        if not self.label.strip():
            raise ValueError("Each matching reference requires a nonempty label")

        object.__setattr__(self, "values", VectorInput(values=self.values).values)

        if not any(self.values):
            raise ValueError(f"{reprlib.repr(self.label)=} should have a positive channel")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceSet:
    entries: tuple[Reference, ...]

    def __post_init__(self) -> None:
        actual_entries = self.entries

        require_type(actual_entries, tuple)

        for entry in self.entries:
            require_type(entry, Reference)

        if not (self.entries):
            raise ValueError(f"{self.entries=} should contain a reference")

        labels = set()
        expected_channel_count = len(self.entries[0].values)

        for reference in self.entries:
            if not ((reference.label not in labels) and (len(reference.values) == expected_channel_count)):
                raise ValueError(
                    f"{reprlib.repr(reference.label)=} should be unique; "
                    f"{len(reference.values)=} should match {expected_channel_count=}"
                )

            labels.add(reference.label)


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchSettings:
    minimum_score: float

    def __post_init__(self) -> None:
        score = _finite(self.minimum_score)
        min_score = 0
        max_score = 1

        if not (min_score <= score <= max_score):
            raise ValueError(f"Expected {min_score=} ≤ {score=} ≤ {max_score=}")

        object.__setattr__(self, "minimum_score", score)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceIndex:
    labels: tuple[str, ...]
    postings: tuple[tuple[tuple[int, float], ...], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchReport:
    label: str
    score: float


def _normalize(channel_weights: tuple[float, ...]) -> tuple[float, ...]:
    scale = max(channel_weights)

    if not (scale != 0):
        raise ScientificRejection("zero_vector", f"{scale=} should be nonzero")

    scaled = []

    for channel_weight in channel_weights:
        scaled.append(channel_weight / scale)

    length = math.hypot(*scaled)
    normalized = []

    for scaled_weight in scaled:
        normalized.append(scaled_weight / length)

    return tuple(normalized)


def build_index(reference_set: ReferenceSet) -> ReferenceIndex:
    """Normalize each reference once and retain only nonzero channel postings."""
    channels = []

    for _ in reference_set.entries[0].values:
        channels.append([])

    labels = []

    for (reference_index, reference) in enumerate(reference_set.entries):
        labels.append(reference.label)

        for (channel, weight) in enumerate(_normalize(reference.values)):
            if weight > 0:
                channels[channel].append((reference_index, weight))

    postings = []

    for channel in channels:
        postings.append(tuple(channel))

    return ReferenceIndex(labels=tuple(labels), postings=tuple(postings))


def match(index: ReferenceIndex, settings: MatchSettings, candidate: VectorInput) -> MatchReport:
    """Return the highest cosine score; exact ties choose the smallest label."""
    expected_channel_count = len(index.postings)

    if not (len(candidate.values) == expected_channel_count):
        raise ScientificRejection(
            "wrong_dimensions",
            f"{len(candidate.values)=} should match {expected_channel_count=}",
        )

    scores = [0.0] * len(index.labels)

    for (channel, weight) in enumerate(_normalize(candidate.values)):
        if weight > 0:
            for (reference_index, reference_weight) in index.postings[channel]:
                scores[reference_index] += weight * reference_weight

    best_index = min(range(len(scores)), key=lambda position: (-scores[position], index.labels[position]))
    score = min(1.0, scores[best_index])

    if not (score != 0):
        raise ScientificRejection(
            "no_overlap",
            f"{score=} should be nonzero",
        )

    if not (score >= settings.minimum_score):
        raise ScientificRejection(
            "below_threshold",
            f"{score=} should reach {settings.minimum_score=}",
        )

    return MatchReport(label=index.labels[best_index], score=score)
