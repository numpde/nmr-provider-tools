"""Bind one indexed matcher and retain its computed index across analyses."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
import tomllib

import provider_core.contribution as contribution
import provider_core.contribution_declaration as declaration

from . import science


@dataclass(slots=True, kw_only=True)
class ReferenceLibrary:
    references: science.ReferenceSet
    settings: science.MatchSettings
    loaded_index: science.ReferenceIndex | None = field(default=None, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceBinding(contribution.ImplementationBinding[ReferenceLibrary, None]):
    match: contribution.CallBinding[
        science.VectorInput, contribution.NoState, science.MatchReport,
    ]


def _load_library(library: ReferenceLibrary) -> None:
    library.loaded_index = science.build_index(library.references)


def _dispose_library(library: ReferenceLibrary) -> None:
    library.loaded_index = None


def create_binding() -> ReferenceBinding:
    """Snapshot deployment configuration without loading or acquiring a library.

    This provider-owned assembly helper is invoked explicitly by startup code
    or tests. The SDK accepts the returned ImplementationBinding; it does not
    discover this function or prescribe its name or signature. Index creation
    remains in the returned instance-initialization hook.
    """
    package = files(__package__)
    configured = tomllib.loads(package.joinpath("settings.toml").read_text(encoding="utf-8"))
    settings = science.MatchSettings(**configured["matching"])
    references = []
    for reference in configured["references"]:
        references.append(science.Reference(label=reference["label"], values=tuple(reference["values"])))
    reference_set = science.ReferenceSet(entries=tuple(references))
    requirement = contribution.ResourceRequirement(
        **tomllib.loads(package.joinpath("resources.toml").read_text(encoding="utf-8"))
    )
    def construct() -> ReferenceLibrary:
        return ReferenceLibrary(settings=settings, references=reference_set)

    def match(
        context: contribution.CallContext[
            ReferenceLibrary, None, science.VectorInput, contribution.NoState,
        ],
    ) -> contribution.ScientificReturn[science.MatchReport]:
        instance = context.instance
        if instance.loaded_index is None:
            raise RuntimeError("Cannot match a query before the reference index has been initialized")
        return contribution.ScientificReturn(
            value=science.match(
                instance.loaded_index, instance.settings, context.scientific_input,
            ),
        )

    match_call = contribution.CallBinding.from_json_dataclasses(
        name="match", input_format="reference.vector.v1", result_format="reference.match-report.v1",
        input_type=science.VectorInput,
        state_type=contribution.NoState,
        result_type=science.MatchReport,
        state_outputs=(), resources=(requirement,), concurrency=contribution.ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=contribution.InterruptionSupport.NONE, progress=False, scientific_call=match,
    )
    configuration = declaration.json_configuration(settings, format_id="reference.settings.v1")
    assets = (
        declaration.json_asset(reference_set, name="references", format_id="reference.library.v1", media_type="application/json"),
        declaration.json_asset(requirement, name="resources", format_id="reference.resources.v1", media_type="application/json"),
    )
    return ReferenceBinding(
        identity=contribution.ConfiguredIdentity(
            binding_id="reference.match",
            implementation="reference.match.v1",
            contract_version=1,
            configuration=configuration,
            assets=assets,
        ),
        match=match_call,
        resident_resources=(), isolation=contribution.IsolationRequirement.SHARED_PROCESS,
        instance=contribution.InstanceHooks(construct=construct, initialize=_load_library, dispose=_dispose_library),
        analysis=None,
    )
