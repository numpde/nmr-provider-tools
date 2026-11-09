"""Host-independent helpers for provider conformance execution."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio

from provider_core.contribution import ImplementationBinding, ResourceRequirement
from provider_core.hosting import ResourceGrant
from provider_core.provider_conformance import (
    CallFixture,
    ConformanceLimits,
    ExpectedSuccess,
    ProviderFixture,
)


def index(records, *, label: str):
    indexed = {record.name: record for record in records}
    if len(indexed) != len(records):
        raise AssertionError(f"Provider fixture has duplicate {label} names")
    return indexed


def create_bindings(case, fixture: ProviderFixture):
    bindings = {}
    for (name, implementation) in index(
        fixture.implementations,
        label="implementation",
    ).items():
        binding = implementation.create_binding()
        case.assertIsInstance(
            binding,
            ImplementationBinding,
            msg=f"Provider implementation {name!r} did not return ImplementationBinding",
        )
        bindings[name] = binding
    return bindings


def _grant_sort_key(identity: tuple[str, str, str | None]) -> tuple[str, str, str]:
    (kind, unit, selector) = identity
    return kind, unit, "" if selector is None else selector


def resource_grants(binding: ImplementationBinding) -> tuple[ResourceGrant, ...]:
    resident = {
        requirement.identity: requirement
        for requirement in binding.description.resident_resources
    }
    active: dict[tuple[str, str, str | None], ResourceRequirement] = {}
    for call in binding.description.calls:
        for requirement in call.resources:
            known = active.get(requirement.identity)
            if known is None or known.amount < requirement.amount:
                active[requirement.identity] = requirement

    grants = []
    for identity in sorted(set(resident) | set(active), key=_grant_sort_key):
        resident_requirement = resident.get(identity)
        active_requirement = active.get(identity)
        requirement = resident_requirement or active_requirement
        grants.append(
            ResourceGrant(
                kind=requirement.kind,
                amount=(
                    (resident_requirement.amount if resident_requirement else 0)
                    + (active_requirement.amount if active_requirement else 0)
                ),
                unit=requirement.unit,
                selector=requirement.selector,
                exclusive=bool(
                    (resident_requirement and resident_requirement.exclusive)
                    or (active_requirement and active_requirement.exclusive)
                ),
            )
        )
    return tuple(grants)


def result_allowance(call: CallFixture, limits: ConformanceLimits) -> int:
    if isinstance(call.expected, ExpectedSuccess):
        return max(limits.minimum_result_bytes, 2 * len(call.expected.result))
    return limits.minimum_result_bytes


async def finished(operation, *, seconds: float):
    async with asyncio.timeout(seconds):
        observation = await operation.observe(after=None, wait_seconds=0)
        while not observation.finished:
            observation = await operation.observe(
                after=observation.revision,
                wait_seconds=1,
            )
    return observation


async def settled(control, *, label: str, seconds: float) -> None:
    async with asyncio.timeout(seconds):
        observation = await control.observe(after=None, wait_seconds=0)
        while not observation.settled and not observation.failures:
            observation = await control.observe(
                after=observation.revision,
                wait_seconds=1,
            )
    if observation.failures:
        raise BaseExceptionGroup(
            f"{label} failed",
            [failure.cause for failure in observation.failures],
        )
    if not observation.settled:
        raise AssertionError(f"{label} did not settle")


def allow() -> None:
    return None


async def close_analysis(analysis, *, label: str, seconds: float) -> None:
    await settled(
        analysis.close(),
        label=label,
        seconds=seconds,
    )


async def retire_host(host, *, label: str, seconds: float) -> None:
    await settled(
        host.retire(),
        label=label,
        seconds=seconds,
    )
