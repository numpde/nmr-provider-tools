"""Register an indexed matcher whose host-owned instance spans analyses."""

from __future__ import annotations

from dataclasses import dataclass

import provider_core.scientific as sdk

from . import science


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceConfig:
    settings: science.MatchSettings
    references: science.ReferenceSet


class ReferenceMatcher(sdk.Contribution):
    def __init__(self, *, configuration: ReferenceConfig) -> None:
        super().__init__()
        self.configuration = configuration

    loaded_index: science.ReferenceIndex | None = None

    def initialize(self, *, assets: sdk.AssetAccess) -> None:
        self.loaded_index = science.build_index(reference_set=self.configuration.references)

    def close(self) -> None:
        self.loaded_index = None

    @sdk.operation()
    def match(self, *, candidate: science.VectorInput) -> science.MatchReport:
        if self.loaded_index is None:
            raise RuntimeError("Cannot match a query before the reference index has been initialized")

        return science.match(
            index=self.loaded_index, settings=self.configuration.settings, candidate=candidate,
        )
