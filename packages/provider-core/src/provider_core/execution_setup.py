"""Decode common execution settings and construct their selected SDK host.

Providers still own scientific construction, topology, readiness and lifetime
entry. This module acquires no worker and starts no scientific operation.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import reprlib
from types import MappingProxyType

from provider_core.contribution import ResourceRequirement
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import OperationPolicy
from provider_core.process_host import ProcessExecutionHost
from provider_core.runner_session import RunnerSession
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
