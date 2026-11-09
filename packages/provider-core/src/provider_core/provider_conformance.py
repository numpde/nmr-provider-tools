"""Reusable execution tests driven by provider-owned scientific fixtures.

A provider supplies binding factories, wire inputs, and expected scientific
outcomes. Shared test code owns allocation, dispatch, observation, result custody,
analysis cleanup, and host retirement.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import Callable
from dataclasses import dataclass

from provider_core.contribution import ImplementationBinding, ProgressEvent


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


class ProviderConformanceTests:
    """Provider-agnostic methods for an IsolatedAsyncioTestCase subclass."""

    def provider_fixture(self) -> ProviderFixture:
        raise NotImplementedError

    def provider_conformance_limits(self) -> ConformanceLimits:
        return ConformanceLimits()

    def test_provider_binding_factories_are_reproducible(self) -> None:
        from provider_core._provider_conformance_support import create_bindings

        fixture = self.provider_fixture()
        first = create_bindings(self, fixture)
        second = create_bindings(self, fixture)

        for implementation in fixture.implementations:
            with self.subTest(implementation=implementation.name):
                self.assertEqual(
                    first[implementation.name].description,
                    second[implementation.name].description,
                )

    async def test_provider_scenarios_end_to_end(self) -> None:
        from provider_core._provider_conformance_execution import run_provider_fixture

        await run_provider_fixture(
            self,
            self.provider_fixture(),
            self.provider_conformance_limits(),
        )
