"""Select the reference library and SDK execution policy for one deployment."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import pathlib
import tomllib

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import OperationPolicy
from provider_core.scientific import HostedContribution, ResourceRequirement

from .binding import ReferenceConfig, ReferenceMatcher
from . import science


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = pathlib.Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """Retain the selected library and policy across independent query analyses."""

    execution_host: InProcessExecutionHost
    matcher: HostedContribution
    operation_policy: OperationPolicy


@asynccontextmanager
async def open_provider() -> AsyncIterator[Provider]:
    """Read configuration once; indexing remains lazy until the first query."""

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)

    operation_policy = OperationPolicy(**provider_config["execution"]["operation_policy"])

    resource_requirements = frozenset(
        ResourceRequirement(**resource_requirement)
        for resource_requirement in provider_config["execution"]["resource_requirement"]
    )

    matching_config = provider_config["science"]["matching"]
    reference_entries = []

    for reference_config in matching_config["references"]:
        reference_entries.append(science.Reference(label=reference_config["label"], values=tuple(reference_config["values"])))

    reference_configuration = ReferenceConfig(
        settings=science.MatchSettings(**matching_config["settings"]),
        references=science.ReferenceSet(entries=tuple(reference_entries)),
    )

    async with InProcessExecutionHost(**provider_config["execution"]["in_process_execution_host"]) as execution_host:
        yield Provider(
            execution_host=execution_host,
            matcher=execution_host.adopt(
                contribution=ReferenceMatcher(configuration=reference_configuration),
                requirements=resource_requirements,
            ),
            operation_policy=operation_policy,
        )
