"""Cosine matching of nonnegative channel vectors using immutable postings."""

from __future__ import annotations

from dataclasses import dataclass
import math

from provider_core.contribution_errors import ScientificRejection


def _finite(value: object) -> float:
    if type(value) not in (int, float):
        raise TypeError("Reference matching requires numeric values; booleans are not measurements")
    if not (math.isfinite(value) and 0 <= value):
        raise ValueError("Reference matching requires finite nonnegative numeric values")
    return float(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class VectorInput:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if type(self.values) is not tuple:
            raise TypeError("A matching vector requires an immutable tuple of values")
        if not self.values:
            raise ValueError("A matching vector requires at least one channel value")
        values = []
        for value in self.values:
            values.append(_finite(value))
        object.__setattr__(self, "values", tuple(values))


@dataclass(frozen=True, slots=True, kw_only=True)
class Reference:
    label: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label.strip():
            raise ValueError("Each matching reference requires a nonempty label")
        object.__setattr__(self, "values", VectorInput(values=self.values).values)
        if not any(self.values):
            raise ValueError("A matching reference requires at least one positive channel value")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceSet:
    entries: tuple[Reference, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or any(type(entry) is not Reference for entry in self.entries):
            raise TypeError("A reference library requires an immutable tuple of Reference records")
        if not self.entries:
            raise ValueError("A reference library requires a nonempty immutable tuple of references")
        labels = set()
        dimensions = len(self.entries[0].values)
        for reference in self.entries:
            if reference.label in labels or len(reference.values) != dimensions:
                raise ValueError("Matching references require unique labels and equal vector dimensions")
            labels.add(reference.label)


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchSettings:
    minimum_score: float

    def __post_init__(self) -> None:
        score = _finite(self.minimum_score)
        if not 0 <= score <= 1:
            raise ValueError("Minimum cosine match score must be between 0 and 1")
        object.__setattr__(self, "minimum_score", score)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceIndex:
    labels: tuple[str, ...]
    postings: tuple[tuple[tuple[int, float], ...], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchReport:
    label: str
    score: float


def _normalize(values: tuple[float, ...]) -> tuple[float, ...]:
    scale = max(values)
    if scale == 0:
        raise ScientificRejection("zero_vector", "Cosine matching requires a nonzero vector")
    scaled = []
    for value in values:
        scaled.append(value / scale)
    length = math.hypot(*scaled)
    normalized = []
    for value in scaled:
        normalized.append(value / length)
    return tuple(normalized)


def build_index(reference_set: ReferenceSet) -> ReferenceIndex:
    """Normalize each reference once and retain only nonzero channel postings."""
    channels = []
    for _ in reference_set.entries[0].values:
        channels.append([])
    labels = []
    for reference_index, reference in enumerate(reference_set.entries):
        labels.append(reference.label)
        for channel, weight in enumerate(_normalize(reference.values)):
            if weight > 0:
                channels[channel].append((reference_index, weight))
    postings = []
    for channel in channels:
        postings.append(tuple(channel))
    return ReferenceIndex(labels=tuple(labels), postings=tuple(postings))


def match(index: ReferenceIndex, settings: MatchSettings, candidate: VectorInput) -> MatchReport:
    """Return the highest cosine score; exact ties choose the smallest label."""
    dimensions = len(index.postings)
    if len(candidate.values) != dimensions:
        raise ScientificRejection(
            "wrong_dimensions",
            f"Matching requires {dimensions} vector values; received {len(candidate.values)}",
        )
    scores = [0.0] * len(index.labels)
    for channel, weight in enumerate(_normalize(candidate.values)):
        if weight > 0:
            for reference_index, reference_weight in index.postings[channel]:
                scores[reference_index] += weight * reference_weight
    best_index = min(range(len(scores)), key=lambda position: (-scores[position], index.labels[position]))
    score = min(1.0, scores[best_index])
    if score == 0:
        raise ScientificRejection("no_overlap", "The query shares no positive channels with the reference library")
    if score < settings.minimum_score:
        raise ScientificRejection("below_threshold", "No reference reaches the configured minimum cosine score")
    return MatchReport(label=index.labels[best_index], score=score)
