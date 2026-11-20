"""Select the reference library and SDK execution policy for one deployment."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import pathlib
import tomllib

from provider_core.contribution import CallBinding
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import OperationPolicy
from provider_core.process_host import ProcessExecutionHost
from provider_core.runner_session import RunnerSession
from provider_core.scientific import HostedContribution, ResourceRequirement

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

    operation_policy = OperationPolicy(**provider_config["execution"]["operation_policy"])

    resource_requirements = frozenset(
        ResourceRequirement(**resource_requirement)
        for resource_requirement in provider_config["execution"]["resource_requirement"]
    )

    matching_config = provider_config["science"]["matching"]
    execution_mode = provider_config["execution"]["backend"]
    host_options = provider_config["execution"]["in_process_execution_host"]

    if execution_mode == "thread":
        if process_session is not None:
            raise ValueError("Cannot start thread matching with a scientific process session; threads own no native worker")
        execution_host = InProcessExecutionHost(**host_options)
    elif execution_mode == "process":
        if process_session is None:
            raise ValueError("Cannot start process matching without a deployment-owned scientific runner session")
        execution_host = ProcessExecutionHost(session=process_session, **host_options)
    else:
        raise ValueError(f"Cannot start reference matching with {execution_mode=}; choose 'thread' or 'process'")

    async with execution_host:
        if execution_mode == "process":
            matcher = await execution_host.start(
                contribution_type=ReferenceMatcher,
                constructor="reference_provider.process_factory:construct",
                startup=matching_config,
                requirements=resource_requirements,
            )
        else:
            matcher = execution_host.adopt(
                contribution=construct(startup=matching_config),
                requirements=resource_requirements,
            )

        yield Provider(
            execution_host=execution_host,
            matcher=matcher,
            match_call=matcher.call(method=ReferenceMatcher.match),
            operation_policy=operation_policy,
        )
