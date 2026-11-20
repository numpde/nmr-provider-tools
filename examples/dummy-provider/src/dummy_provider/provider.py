"""Select the weighted science and own its SDK execution lifetime."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import pathlib
import tomllib

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import OperationPolicy
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

    return frozenset(
        ResourceRequirement(**requirement)
        for requirement in configuration["execution"]["resource_requirement"]
    )


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
    backend = execution["backend"]
    operation_policy = OperationPolicy(**execution["operation_policy"])
    requirements = frozenset(
        ResourceRequirement(**requirement)
        for requirement in execution["resource_requirement"]
    )
    weighted = configuration["science"]["weighted"]

    if backend == "thread":
        if process_session is not None:
            raise ValueError("Cannot start thread execution with a scientific process session; threads do not use that session")

        execution_host = InProcessExecutionHost(**execution["host"])
    elif backend == "process":
        if process_session is None:
            raise ValueError("Cannot start process execution without a deployment-owned scientific runner session")

        execution_host = ProcessExecutionHost(session=process_session, **execution["host"])
    else:
        raise ValueError(f"Cannot start dummy provider with {backend = !r}; choose 'thread' or 'process'")

    async with execution_host:
        if backend == "thread":
            hosted = execution_host.adopt(
                contribution=construct(startup=weighted),
                requirements=requirements,
            )
            await execution_host.initialize(allocation=hosted.allocation)
        else:
            hosted = await execution_host.start(
                contribution_type=WeightedSum,
                constructor="dummy_provider.process_factory:construct",
                startup=weighted,
                requirements=requirements,
            )

        yield Provider(execution_host=execution_host, weighted=hosted, operation_policy=operation_policy)
