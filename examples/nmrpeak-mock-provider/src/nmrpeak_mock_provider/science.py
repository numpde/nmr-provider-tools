"""Scientific inputs, model interface and validated input ownership.

HF uses formula and proton peaks; CHF also uses carbon peaks. These records
contain scientific values, without Jobs, transports or execution handles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol

from provider_core.contribution_errors import ScientificRejection
from provider_core.type_requirements import require_type


@dataclass(frozen=True, slots=True, kw_only=True)
class ProtonPeak:
    centroid: str
    integral: int
    multiplicity: str
    couplings: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class HfInput:
    """A formula and unassigned proton peaks proposed for the HF model."""

    formula: str
    proton_peaks: tuple[ProtonPeak, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ChfInput:
    """A formula, proton peaks and carbon shifts proposed for the CHF model."""

    formula: str
    proton_peaks: tuple[ProtonPeak, ...]
    carbon_peaks: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """Checkpoint digest and limits for preparation and generated candidates."""

    checkpoint_sha256: str
    maximum_input_tokens: int
    beam_size: int
    maximum_candidate_characters: int

    def __post_init__(self) -> None:
        for model_limit in (self.maximum_input_tokens, self.beam_size, self.maximum_candidate_characters):
            require_type(model_limit, int)

            if not (0 < model_limit):
                raise ValueError(f"{model_limit=} should be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparationReport:
    """Token count after preparation; generation uses the separately retained state."""

    token_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidates:
    """Admitted model output and the digest of the checkpoint that generated it."""

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

    tokens: tuple[str, ...]

    def close(self) -> None:
        self.tokens = ()


def prepare(*, model: Model, candidate: HfInput | ChfInput, settings: Settings) -> PreparedInput:
    """Use the loaded tokenizer to reject an input before generation starts."""

    if not (candidate.formula):
        raise ScientificRejection(code="missing_science", message=f"{candidate.formula=} should be nonempty")

    if not (candidate.proton_peaks):
        raise ScientificRejection(code="missing_science", message=f"{candidate.proton_peaks=} should be nonempty")

    # repo-check: retain-type-guard -- Carbon peaks are required only for this scientific input variant
    if (isinstance(candidate, ChfInput)) and (not candidate.carbon_peaks):
        raise ScientificRejection(code="missing_carbon", message=f"{candidate.carbon_peaks=} should be nonempty")

    tokens = model.tokenize(candidate=candidate)
    token_count = len(tokens)

    if not (tokens):
        raise ScientificRejection(code="token_limit", message=f"{token_count=} should be positive")

    if not (token_count <= settings.maximum_input_tokens):
        raise ScientificRejection(
            code="token_limit",
            message=f"{token_count=} should be no greater than {settings.maximum_input_tokens=}",
        )

    return PreparedInput(tokens=tokens)


def dispose_prepared(prepared_input: PreparedInput) -> None:
    prepared_input.close()


def validate_candidates(*, candidates: tuple[str, ...], settings: Settings) -> None:
    """Bad model output is an operational failure, never a user correction."""

    require_type(candidates, tuple)

    candidate_count = len(candidates)

    if not ((min_candidate_count := 1) <= candidate_count <= settings.beam_size):
        raise ValueError(f"{candidate_count=} should be between {min_candidate_count=} and {settings.beam_size=}")

    for candidate_smiles in candidates:
        require_type(candidate_smiles, str)

        candidate_character_count = len(candidate_smiles)

        if not ((min_candidate_characters := 1) <= candidate_character_count <= settings.maximum_candidate_characters):
            raise ValueError(
                f"{candidate_character_count=} should be between {min_candidate_characters=} "
                f"and {settings.maximum_candidate_characters=}"
            )
