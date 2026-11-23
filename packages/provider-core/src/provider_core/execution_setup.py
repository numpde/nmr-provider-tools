"""Decode common execution settings and construct their selected SDK host.

Providers select scientific declarations and deployment settings. Explicit host
construction supports custom topologies; open_process_call owns the standard
single-model runner lifetime.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import AsyncIterator, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
import reprlib
from types import MappingProxyType

from provider_core.contribution import ResourceRequirement
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import AdmissionCode, OperationPolicy
from provider_core.process_host import ProcessExecutionHost
from provider_core.runner_session import RunnerSession
from provider_core.scientific import Contribution, HostedContribution
from provider_core.scientific_call import ErrorPolicy, ScientificCall
from provider_core.type_requirements import require_instance, require_type


def parse_resource_requirements(requirements) -> frozenset[ResourceRequirement]:
    """Project the resource-only field for full startup or direct binding callers."""
    return frozenset(ResourceRequirement(**requirement) for requirement in requirements)


@dataclass(frozen=True, slots=True, init=False)
class ExecutionSettings:
    """Snapshot the shared execution section while leaving provider extensions local."""

    backend: str
    host: Mapping
    operation_policy: OperationPolicy
    error_policy: ErrorPolicy
    resource_requirements: frozenset[ResourceRequirement]

    def __init__(self, execution: Mapping):
        require_instance(execution, Mapping)
        backend = execution["backend"]
        require_type(backend, str)

        if not (backend in (expected_backends := {"thread", "process"})):
            raise ValueError(f"{reprlib.repr(backend)=} ∉ {expected_backends=}")

        require_instance(execution["host"], Mapping)
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "host", MappingProxyType(dict(execution["host"])))
        object.__setattr__(self, "operation_policy", OperationPolicy(**execution["operation_policy"]))
        object.__setattr__(self, "resource_requirements", parse_resource_requirements(execution["resource_requirement"]))

        error_policy = ErrorPolicy()

        if "error_policy" in execution:
            error_options = dict(execution["error_policy"])

            if "retry_admission_codes" in error_options:
                error_options["retry_admission_codes"] = frozenset(AdmissionCode(code) for code in error_options["retry_admission_codes"])

            error_policy = ErrorPolicy(**error_options)

        object.__setattr__(self, "error_policy", error_policy)

    def bind_call(
        self,
        *,
        host: InProcessExecutionHost | ProcessExecutionHost,
        contribution: HostedContribution,
        method: Callable,
    ) -> ScientificCall:
        """Bind a typed method to the deployment's execution and recovery policy."""

        return ScientificCall(
            host=host,
            allocation=contribution.allocation,
            binding=contribution.call(method=method),
            operation_policy=self.operation_policy,
            error_policy=self.error_policy,
        )

    async def start_contribution(
        self,
        *,
        host: InProcessExecutionHost | ProcessExecutionHost,
        contribution_type: type[Contribution],
        constructor: Callable,
        startup: Mapping,
    ) -> HostedContribution:
        """Initialize selected science inside an already owned execution host."""

        if self.backend == "process":
            return await host.start(
                contribution_type=contribution_type,
                constructor=host._member_path(constructor),
                startup=startup,
                requirements=self.resource_requirements,
            )

        contribution = host.adopt(
            contribution=constructor(startup=startup),
            requirements=self.resource_requirements,
        )

        await host.initialize(allocation=contribution.allocation)

        return contribution

    @asynccontextmanager
    async def open_contribution(
        self,
        *,
        contribution_type: type[Contribution],
        constructor: Callable,
        startup: Mapping,
        process_session: RunnerSession | None = None,
        host: Mapping | None = None,
    ) -> AsyncIterator[tuple[InProcessExecutionHost | ProcessExecutionHost, HostedContribution]]:
        """Own host startup and retirement for one configured contribution.

        Injected process sessions transfer custody on successful host construction,
        just as with create_host(). Earlier configuration failures leave them with
        the caller. Both backends initialize science before yielding.
        """
        execution_host = self.create_host(process_session=process_session, host=host)

        async with execution_host:
            contribution = await self.start_contribution(
                host=execution_host,
                contribution_type=contribution_type,
                constructor=constructor,
                startup=startup,
            )

            yield (execution_host, contribution)

    @asynccontextmanager
    async def open_process_call(
        self,
        *,
        runner: Mapping,
        contribution_type: type[Contribution],
        constructor: Callable,
        startup: Mapping,
        method: Callable,
    ) -> AsyncIterator[ScientificCall]:
        """Own the runner, model and callable until all work and retirement settle.

        The factory runs in the scientific process. Errors during setup or in
        the caller's context still retire the host and close its runner session.
        """

        if not (self.backend == "process"):
            raise ValueError(f"{self.backend=} should be 'process' to open a runner-backed scientific call")

        session = RunnerSession.from_config(runner)

        try:
            host = self.create_host(process_session=session)
        except BaseException:
            session.close()

            raise

        async with host:
            contribution = await self.start_contribution(
                host=host,
                contribution_type=contribution_type,
                constructor=constructor,
                startup=startup,
            )

            yield self.bind_call(host=host, contribution=contribution, method=method)

    def require_sessions(self, *, process_sessions: Mapping | None, expected_keys: Collection) -> None:
        """Check a provider's inventory before any host takes session custody.

        Keys belong to provider assembly. This preflight neither inspects session
        state nor connects or closes sessions; each host still checks freshness.
        """
        if self.backend == "thread":
            require_type(process_sessions, type(None), error_factory=ValueError)

            return

        if not (process_sessions is not None):
            raise ValueError(f"{process_sessions=} should map configured keys to runner sessions")

        require_instance(process_sessions, Mapping)
        expected_session_keys = set(expected_keys)
        supplied_session_keys = set(process_sessions)

        if not (supplied_session_keys == expected_session_keys):
            raise ValueError(f"{reprlib.repr(supplied_session_keys)=} ≠ {reprlib.repr(expected_session_keys)=}")

        distinct_session_count = len({id(session) for session in process_sessions.values()})

        if not (distinct_session_count == len(process_sessions)):
            raise ValueError(f"{distinct_session_count=} should equal {len(process_sessions)=}")

    # silent-defaults: allow-contract -- Threads take no session; omitted host overrides use this settings snapshot.
    def create_host(
        self,
        *,
        process_session: RunnerSession | None = None,
        host: Mapping | None = None,
    ) -> InProcessExecutionHost | ProcessExecutionHost:
        """Construct only; the caller immediately enters the returned host lifetime.

        Successful process-host construction transfers session custody. Refusal
        leaves it with its caller. No adoption, initialization or start occurs here.
        """
        host_options = self.host if host is None else host

        if self.backend == "thread":
            require_type(process_session, type(None), error_factory=ValueError)

            return InProcessExecutionHost(**host_options)

        if not (process_session is not None):
            raise ValueError(f"{process_session=} should be a {RunnerSession=}")

        return ProcessExecutionHost(session=process_session, **host_options)
