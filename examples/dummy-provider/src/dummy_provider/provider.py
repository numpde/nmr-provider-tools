"""Select the weighted science and own its SDK execution lifetime."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import pathlib
import tomllib

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import OperationPolicy
from provider_core.execution_setup import ExecutionSettings, parse_resource_requirements
from provider_core.process_host import ProcessExecutionHost
from provider_core.runner_session import RunnerSession
from provider_core.scientific import HostedContribution, ResourceRequirement

from .binding import WeightedSum
from .process_factory import construct


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = pathlib.Path("config/deployments/localhost/provider.toml")


def read_resource_requirements() -> frozenset[ResourceRequirement]:
    """Read the deployment requirements used by direct binding examples."""

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        configuration = tomllib.load(source)

    return parse_resource_requirements(configuration["execution"]["resource_requirement"])


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    execution_host: InProcessExecutionHost | ProcessExecutionHost
    weighted: HostedContribution
    operation_policy: OperationPolicy


@asynccontextmanager
async def open_provider(
    *,
    # silent-defaults: allow-contract -- Thread startup needs no native session; process startup requires an explicit deployment-owned session.
    process_session: RunnerSession | None = None,
) -> AsyncIterator[Provider]:
    """Read configuration once and initialize the selected model before yielding.

    A process session remains caller-owned until host construction. Configuration
    refusals before that point do not connect or close it. The caller finishes
    analyses before leaving this context; the SDK owns operation and state cleanup.
    """

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        configuration = tomllib.load(source)

    execution = configuration["execution"]
    settings = ExecutionSettings(execution)
    weighted = configuration["science"]["weighted"]
    async with settings.open_contribution(
        contribution_type=WeightedSum,
        constructor=construct,
        startup=weighted,
        process_session=process_session,
    ) as (execution_host, hosted):
        yield Provider(execution_host=execution_host, weighted=hosted, operation_policy=settings.operation_policy)
