"""Register an indexed matcher whose host-owned instance spans analyses."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import tomllib

import provider_core.scientific as sdk
from provider_core.embedded_host import EmbeddedHost

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


def create_binding(*, host: EmbeddedHost) -> sdk.HostedContribution:
    """Provider-owned assembly, invoked explicitly; no index is loaded here."""
    package = files(anchor=__package__)
    configured = tomllib.loads(package.joinpath("settings.toml").read_text(encoding="utf-8"))
    settings = science.MatchSettings(**configured["matching"])
    references = []
    for reference in configured["references"]:
        references.append(science.Reference(label=reference["label"], values=tuple(reference["values"])))
    reference_set = science.ReferenceSet(entries=tuple(references))
    requirement = sdk.ResourceRequirement(
        **tomllib.loads(package.joinpath("resources.toml").read_text(encoding="utf-8"))
    )
    return host.adopt(
        contribution=ReferenceMatcher(configuration=ReferenceConfig(settings=settings, references=reference_set)),
        requirements=(requirement,),
    )
