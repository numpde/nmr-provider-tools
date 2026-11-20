"""Small deterministic substitutes for NMRPeak's tokenizer and neural model.

The calling interfaces and loading order are ordinary scientific boundaries.
The tokenization and rankings are synthetic and have no chemical validity.
"""

import json
from typing import BinaryIO

from .science import ChfInput, HfInput


class Tokenizer:
    """Flatten mock input into deterministic tokens without chemical interpretation."""

    def tokenize(self, *, candidate: HfInput | ChfInput) -> tuple[str, ...]:
        tokens = [candidate.formula]

        for peak in candidate.proton_peaks:
            tokens.extend((peak.centroid, str(peak.integral), peak.multiplicity, *peak.couplings))

        if isinstance(candidate, ChfInput):
            tokens.extend(candidate.carbon_peaks)

        return tuple(tokens)


class Model:
    """Retain each loaded component immediately, including partial loading."""

    def __init__(self) -> None:
        self.tokenizer: Tokenizer | None = None
        self.candidates: tuple[str, ...] | None = None

    def load(self, *, checkpoint: BinaryIO) -> None:
        self.tokenizer = Tokenizer()

        model_configuration = json.load(fp=checkpoint)
        self.candidates = tuple(model_configuration["candidates"])

    def tokenize(self, *, candidate: HfInput | ChfInput) -> tuple[str, ...]:
        if not (self.tokenizer is not None):
            raise RuntimeError(f"{self.tokenizer=} should be initialized")

        return self.tokenizer.tokenize(candidate=candidate)

    def generate(self, *, tokens: tuple[str, ...], beam_size: int) -> tuple[str, ...]:
        if not (self.candidates is not None):
            raise RuntimeError(f"{self.candidates=} should be loaded")

        offset = sum(len(token) for token in tokens) % len(self.candidates)
        ordered = self.candidates[offset:] + self.candidates[:offset]

        return ordered[:beam_size]

    def close(self) -> None:
        self.tokenizer = None
        self.candidates = None
