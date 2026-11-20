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

        if backend not in {"thread", "process"}:
            raise ValueError(f"Cannot select execution with backend={reprlib.repr(backend)}; choose 'thread' or 'process'")

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
            if process_sessions is not None:
                raise ValueError("Thread execution does not use scientific process sessions")
            return

        if process_sessions is None:
            raise ValueError("Process execution requires deployment-owned scientific runner sessions")

        require_instance(process_sessions, Mapping)
        expected = set(expected_keys)
        if set(process_sessions) != expected:
            raise ValueError(
                f"Cannot configure process execution with session keys {reprlib.repr(set(process_sessions))}; "
                f"supply exactly one runner session for each configured key {reprlib.repr(expected)}"
            )
        if len({id(session) for session in process_sessions.values()}) != len(process_sessions):
            raise ValueError("Cannot configure process execution: independent hosts require distinct runner sessions")

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
        options = self.host if host is None else host

        if self.backend == "thread":
            if process_session is not None:
                raise ValueError("Cannot construct thread execution with a process session; threads do not use native runner sessions")

            return InProcessExecutionHost(**options)

        if process_session is None:
            raise ValueError("Cannot construct process execution without a deployment-owned scientific runner session")

        return ProcessExecutionHost(session=process_session, **options)
