"""Select the reference library and SDK execution policy for one deployment."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import tomllib

from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import OperationPolicy
from provider_core.scientific import HostedContribution, ResourceRequirement

from .binding import ReferenceConfig, ReferenceMatcher
from . import science


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """Retain the selected library and policy across independent query analyses."""

    host: EmbeddedHost
    matcher: HostedContribution
    operation_policy: OperationPolicy


@asynccontextmanager
async def open_provider() -> AsyncIterator[Provider]:
    """Read configuration once; indexing remains lazy until the first query."""
    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        configuration = tomllib.load(source)
    policy = OperationPolicy(**configuration["execution"]["operation_policy"])
    requirements = frozenset(
        ResourceRequirement(**requirement)
        for requirement in configuration["execution"]["resource_requirement"]
    )
    matching = configuration["science"]["matching"]
    references = []
    for reference in matching["references"]:
        references.append(science.Reference(label=reference["label"], values=tuple(reference["values"])))
    scientific_configuration = ReferenceConfig(
        settings=science.MatchSettings(**matching["settings"]),
        references=science.ReferenceSet(entries=tuple(references)),
    )
    async with EmbeddedHost(**configuration["execution"]["embedded_host"]) as host:
        yield Provider(
            host=host,
            matcher=host.adopt(
                contribution=ReferenceMatcher(configuration=scientific_configuration),
                requirements=requirements,
            ),
            operation_policy=policy,
        )
