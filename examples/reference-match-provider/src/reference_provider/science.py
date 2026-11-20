"""Cosine matching of nonnegative channel vectors using immutable postings."""

from __future__ import annotations

from dataclasses import dataclass
import math
import reprlib

from provider_core.contribution_errors import ScientificRejection
from provider_core.tools import require_nonnegative_number
from provider_core.type_requirements import require_type


@dataclass(frozen=True, slots=True, kw_only=True)
class VectorInput:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        require_type(self.values, tuple)

        if not (self.values):
            raise ValueError(f"{self.values=} should be nonempty")

        finite_channels = []

        for channel in self.values:
            finite_channels.append(float(require_nonnegative_number(channel)))

        object.__setattr__(self, "values", tuple(finite_channels))


@dataclass(frozen=True, slots=True, kw_only=True)
class Reference:
    label: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        require_type(self.label, str)

        if not self.label.strip():
            raise ValueError(f"{reprlib.repr(self.label)=} should be nonblank")

        object.__setattr__(self, "values", VectorInput(values=self.values).values)

        if not any(self.values):
            raise ValueError(f"{reprlib.repr(self.label)=} should have a positive channel")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceSet:
    entries: tuple[Reference, ...]

    def __post_init__(self) -> None:
        require_type(self.entries, tuple)

        for entry in self.entries:
            require_type(entry, Reference)

        if not (self.entries):
            raise ValueError(f"{self.entries=} should contain a reference")

        labels = set()
        expected_channel_count = len(self.entries[0].values)

        for reference in self.entries:
            if not (reference.label not in labels):
                raise ValueError(f"{reprlib.repr(reference.label)=} should be unique")

            if not (len(reference.values) == expected_channel_count):
                raise ValueError(f"{len(reference.values)=} should match {expected_channel_count=}")

            labels.add(reference.label)


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchSettings:
    minimum_score: float

    def __post_init__(self) -> None:
        minimum_score = float(require_nonnegative_number(self.minimum_score))
        max_score = 1

        if not (minimum_score <= max_score):
            raise ValueError(f"{minimum_score=} should be no greater than {max_score=}")

        object.__setattr__(self, "minimum_score", minimum_score)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceIndex:
    labels: tuple[str, ...]
    postings: tuple[tuple[tuple[int, float], ...], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchReport:
    label: str
    score: float


def _normalize(channel_weights: tuple[float, ...]) -> tuple[float, ...]:
    max_channel_weight = max(channel_weights)

    if not (max_channel_weight != 0):
        raise ScientificRejection(code="zero_vector", message=f"{max_channel_weight=} should be nonzero")

    scaled = []

    for channel_weight in channel_weights:
        scaled.append(channel_weight / max_channel_weight)

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
            code="wrong_dimensions",
            message=f"{len(candidate.values)=} should match {expected_channel_count=}",
        )

    scores = [0.0] * len(index.labels)

    for (channel, weight) in enumerate(_normalize(candidate.values)):
        if weight > 0:
            for (reference_index, reference_weight) in index.postings[channel]:
                scores[reference_index] += weight * reference_weight

    best_index = min(range(len(scores)), key=lambda position: (-scores[position], index.labels[position]))
    cosine_score = min(1.0, scores[best_index])

    if not (cosine_score != 0):
        raise ScientificRejection(
            code="no_overlap",
            message=f"{cosine_score=} should be nonzero",
        )

    if not (cosine_score >= settings.minimum_score):
        raise ScientificRejection(
            code="below_threshold",
            message=f"{cosine_score=} should reach {settings.minimum_score=}",
        )

    return MatchReport(label=index.labels[best_index], score=cosine_score)
