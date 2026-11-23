"""Select the reference library and SDK execution policy for one deployment."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import pathlib
import tomllib

from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import OperationPolicy
from provider_core.execution_setup import ExecutionSettings
from provider_core.process_host import ProcessExecutionHost
from provider_core.runner_session import RunnerSession
from provider_core.scientific import HostedContribution
from provider_core.scientific_call import ScientificCall

from .binding import ReferenceMatcher
from .process_factory import construct
from .science import MatchReport, VectorInput


# Select the deployment document relative to the application's working directory.
# repo-check: allow-module-constant
PROVIDER_CONFIG_PATH = pathlib.Path("config/deployments/localhost/provider.toml")


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Provider:
    """Retain the selected library and policy across independent query analyses."""

    execution_host: InProcessExecutionHost | ProcessExecutionHost
    matcher: HostedContribution
    match: ScientificCall[VectorInput, MatchReport]
    operation_policy: OperationPolicy


@asynccontextmanager
async def open_provider(*, process_session: RunnerSession | None = None) -> AsyncIterator[Provider]:
    """Own one reusable matcher and its SDK host until retirement settles.

    The SDK initializes the index before yielding on either backend. A process
    deployment supplies its native session; host construction transfers custody.
    """

    with PROVIDER_CONFIG_PATH.open(mode="rb") as source:
        provider_config = tomllib.load(source)

    settings = ExecutionSettings(provider_config["execution"])
    matching_config = provider_config["science"]["matching"]

    async with settings.open_contribution(
        contribution_type=ReferenceMatcher,
        constructor=construct,
        startup=matching_config,
        process_session=process_session,
    ) as (execution_host, matcher):
        yield Provider(
            execution_host=execution_host,
            matcher=matcher,
            match=settings.bind_call(host=execution_host, contribution=matcher, method=ReferenceMatcher.match),
            operation_policy=settings.operation_policy,
        )
