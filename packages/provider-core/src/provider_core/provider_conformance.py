"""Reusable execution tests driven by provider-owned scientific fixtures.

A provider supplies binding factories, wire inputs, and expected scientific
outcomes. The shared test owns allocation, dispatch, observation, result custody,
analysis cleanup, and host retirement.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from provider_core.contribution import (
    CallRef,
    ImplementationBinding,
    ProgressEvent,
    ResourceRequirement,
    ScientificRejection,
)
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.hosting import (
    ExecutionLimits,
    Invocation,
    NotAdmitted,
    Outcome,
    ResourceGrant,
    ResourceState,
    ResultState,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ImplementationFixture:
    name: str
    create_binding: Callable[[], ImplementationBinding]


@dataclass(frozen=True, slots=True, kw_only=True)
class AllocationFixture:
    name: str
    implementation: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedSuccess:
    result: bytes
    progress: tuple[ProgressEvent, ...] = ()
    save_states_as: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedScientificRejection:
    code: str


type ExpectedCall = ExpectedSuccess | ExpectedScientificRejection


@dataclass(frozen=True, slots=True, kw_only=True)
class CallFixture:
    operation_id: str
    allocation: str
    call: str
    payload: bytes
    expected: ExpectedCall
    states_from: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisFixture:
    name: str
    allocations: tuple[str, ...]
    calls: tuple[CallFixture, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderFixture:
    name: str
    implementations: tuple[ImplementationFixture, ...]
    allocations: tuple[AllocationFixture, ...]
    analyses: tuple[AnalysisFixture, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ConformanceLimits:
    queue_seconds: float = 5
    execution_seconds: float = 30
    cleanup_seconds: float = 10
    initialization_seconds: float = 30
    retirement_seconds: float = 10
    observation_seconds: float = 60
    minimum_result_bytes: int = 1024


def _index(records, *, label: str):
    indexed = {record.name: record for record in records}
    if len(indexed) != len(records):
        raise AssertionError(f"Provider fixture has duplicate {label} names")
    return indexed


def _grant_sort_key(identity: tuple[str, str, str | None]) -> tuple[str, str, str]:
    (kind, unit, selector) = identity
    return kind, unit, "" if selector is None else selector


def _resource_grants(binding: ImplementationBinding) -> tuple[ResourceGrant, ...]:
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

    def grant(identity: tuple[str, str, str | None]) -> ResourceGrant:
        resident_requirement = resident.get(identity)
        active_requirement = active.get(identity)
        requirement = resident_requirement or active_requirement

        return ResourceGrant(
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

    return tuple(
        grant(identity)
        for identity in sorted(
            set(resident) | set(active),
            key=_grant_sort_key,
        )
    )


def _result_allowance(call: CallFixture, limits: ConformanceLimits) -> int:
    if isinstance(call.expected, ExpectedSuccess):
        return max(
            limits.minimum_result_bytes,
            2 * len(call.expected.result),
        )
    return limits.minimum_result_bytes


async def _finished(operation, *, seconds: float):
    async with asyncio.timeout(seconds):
        observation = await operation.observe(after=None, wait_seconds=0)
        while not observation.finished:
            observation = await operation.observe(
                after=observation.revision,
                wait_seconds=1,
            )
    return observation


async def _settled(control, *, label: str, seconds: float) -> None:
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


async def _close_analysis(analysis, *, label: str, seconds: float) -> None:
    await _settled(
        analysis.close(),
        label=label,
        seconds=seconds,
    )


async def _retire_host(host, *, label: str, seconds: float) -> None:
    await _settled(
        host.retire(),
        label=label,
        seconds=seconds,
    )


def _allow() -> None:
    return None


class ProviderConformanceTests:
    """Provider-agnostic test methods for an IsolatedAsyncioTestCase subclass."""

    def provider_fixture(self) -> ProviderFixture:
        raise NotImplementedError

    def provider_conformance_limits(self) -> ConformanceLimits:
        return ConformanceLimits()

    def _bindings(self, fixture: ProviderFixture):
        implementations = _index(
            fixture.implementations,
            label="implementation",
        )
        bindings = {}
        for (name, implementation) in implementations.items():
            binding = implementation.create_binding()
            self.assertIsInstance(
                binding,
                ImplementationBinding,
                msg=(
                    f"Provider implementation {name!r} did not return "
                    "ImplementationBinding"
                ),
            )
            bindings[name] = binding
        return bindings

    def test_provider_binding_factories_are_reproducible(self) -> None:
        fixture = self.provider_fixture()
        first = self._bindings(fixture)
        second = self._bindings(fixture)

        for implementation in fixture.implementations:
            with self.subTest(implementation=implementation.name):
                self.assertEqual(
                    first[implementation.name].description,
                    second[implementation.name].description,
                )

    async def test_provider_scenarios_end_to_end(self) -> None:
        fixture