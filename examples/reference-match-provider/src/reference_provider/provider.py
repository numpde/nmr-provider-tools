"""Select the reference library and SDK execution policy for one deployment."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import pathlib
import tomllib

from provider_core.contribution import CallBinding
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import OperationPolicy
from provider_core.execution_setup import ExecutionSettings
from provider_core.process_host import ProcessExecutionHost
from provider_core.runner_session import RunnerSession
from provider_core.scientific import HostedContribution

from .binding import ReferenceMatcher
from .process_factory import construct


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = pathlib.Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """Retain the selected library and policy across independent query analyses."""

    execution_host: InProcessExecutionHost | ProcessExecutionHost
    matcher: HostedContribution
    match_call: CallBinding
    operation_policy: OperationPolicy


@asynccontextmanager
async def open_provider(*, process_session: RunnerSession | None = None) -> AsyncIterator[Provider]:
    """Own one reusable matcher and its SDK host until retirement settles.

    Threads build the index on first use; process startup initializes its child
    before yielding. A process deployment supplies the native runner session;
    constructing its host transfers that session to SDK ownership.
    """

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)

    settings = ExecutionSettings(provider_config["execution"])
    matching_config = provider_config["science"]["matching"]
    execution_host = settings.create_host(process_session=process_session)

    async with execution_host:
        if settings.backend == "process":
            matcher = await execution_host.start(
                contribution_type=ReferenceMatcher,
                constructor="reference_provider.process_factory:construct",
                startup=matching_config,
                requirements=settings.resource_requirements,
            )
        else:
            matcher = execution_host.adopt(
                contribution=construct(startup=matching_config),
                requirements=settings.resource_requirements,
            )

        yield Provider(
            execution_host=execution_host,
            matcher=matcher,
            match_call=matcher.call(method=ReferenceMatcher.match),
            operation_policy=settings.operation_policy,
        )
