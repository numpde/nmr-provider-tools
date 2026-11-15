"""Scientific inputs, model interface and validated input ownership.

HF uses formula and proton peaks; CHF also uses carbon peaks. These records
contain scientific values, without Jobs, transports or execution handles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol

from provider_core.contribution_errors import ScientificRejection


@dataclass(frozen=True, slots=True, kw_only=True)
class ProtonPeak:
    centroid: str
    integral: int
    multiplicity: str
    couplings: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class HfInput:
    formula: str
    proton_peaks: tuple[ProtonPeak, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ChfInput:
    formula: str
    proton_peaks: tuple[ProtonPeak, ...]
    carbon_peaks: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    checkpoint_sha256: str
    maximum_input_tokens: int
    beam_size: int
    maximum_candidate_characters: int

    def __post_init__(self) -> None:
        for value in (self.maximum_input_tokens, self.beam_size, self.maximum_candidate_characters):
            if type(value) is not int:
                raise TypeError(f"Unexpected {type(value)=}; model limits require integers")

            if not (0 < value):
                raise ValueError(f"Model limits must be positive; received {value=}")


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationReport:
    token_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidates:
    generated_smiles: tuple[str, ...]
    checkpoint_sha256: str


class Model(Protocol):
    """Empty owner first; loading may partially initialize it before failing."""

    def load(self, *, checkpoint: BinaryIO) -> None: ...
    def tokenize(self, *, candidate: HfInput | ChfInput) -> tuple[str, ...]: ...
    def generate(self, *, tokens: tuple[str, ...], beam_size: int) -> tuple[str, ...]: ...
    def close(self) -> None: ...


@dataclass(slots=True, kw_only=True)
class PreparedInput:
    """The validated input retained until one generation or analysis cleanup."""

    tokens: tuple[str, ...] | None

    def consume(self) -> tuple[str, ...]:
        if self.tokens is None:
            raise RuntimeError("Validated input was already consumed or disposed")

        tokens = self.tokens
        self.tokens = None
        return tokens

    def close(self) -> None:
        self.tokens = None


def prepare(*, model: Model, candidate: HfInput | ChfInput, settings: Settings) -> PreparedInput:
    """Use the loaded tokenizer to reject an input before generation starts."""

    if not candidate.formula or not candidate.proton_peaks:
        raise ScientificRejection(code="missing_science", message="Supply a formula and proton peaks")

    if isinstance(candidate, ChfInput) and not candidate.carbon_peaks:
        raise ScientificRejection(code="missing_carbon", message="CHF requires carbon peaks")

    tokens = model.tokenize(candidate=candidate)
    token_count = len(tokens)

    if not tokens or token_count > settings.maximum_input_tokens:
        raise ScientificRejection(
            code="token_limit",
            message=f"Cannot generate with {token_count=}; requires 1 through {settings.maximum_input_tokens=}",
        )

    return PreparedInput(tokens=tokens)


def dispose_prepared(value: PreparedInput) -> None:
    value.close()


def validate_candidates(*, candidates: tuple[str, ...], settings: Settings) -> None:
    """Bad model output is an operational failure, never a user correction."""

    if type(candidates) is not tuple:
        raise TypeError(f"Unexpected {type(candidates)=}; model output must be a tuple")

    candidate_count = len(candidates)

    if not 1 <= candidate_count <= settings.beam_size:
        raise ValueError(f"Invalid model output: {candidate_count=} exceeds the range 1 through {settings.beam_size=}")

    for candidate in candidates:
        if type(candidate) is not str:
            raise TypeError(f"Unexpected {type(candidate)=}; generated candidates must be strings")

        character_count = len(candidate)

        if not 1 <= character_count <= settings.maximum_candidate_characters:
            raise ValueError(f"Invalid model output: {character_count=} exceeds the range 1 through {settings.maximum_candidate_characters=}")
