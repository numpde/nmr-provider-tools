"""Execute provider-owned conformance scenarios through the shared host."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from provider_core.contribution import (
    CallRef,
    ImplementationBinding,
    ScientificRejection,
)
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.hosting import (
    ExecutionLimits,
    Invocation,
    NotAdmitted,
    Outcome,
    ResourceState,
    ResultState,
)
from provider_core.testing._support import (
    allow,
    close_analysis,
    create_bindings,
    finished,
    index,
    resource_grants,
    result_allowance,
    retire_host,
    settled,
)
from provider_core.testing.conformance import (
    AnalysisFixture,
    CallFixture,
    ConformanceLimits,
    ExpectedScientificRejection,
    ExpectedSuccess,
    ProviderFixture,
)


class _Runner:
    def __init__(
        self,
        case,
        fixture: ProviderFixture,
        limits: ConformanceLimits,
    ) -> None:
        self.case = case
        self.fixture = fixture
        self.limits = limits
        self.bindings = create_bindings(case, fixture)
        self.allocations = index(fixture.allocations, label="allocation")
        self.calls = tuple(
            call
            for analysis in fixture.analyses
            for call in analysis.calls
        )
        case.assertTrue(fixture.analyses, msg="Provider fixture has no analyses")
        case.assertTrue(self.calls, msg="Provider fixture has no scientific calls")

        for allocation in fixture.allocations:
            case.assertIn(
                allocation.implementation,
                self.bindings,
                msg=f"Allocation {allocation.name!r} names an unknown implementation",
            )

        state_values = sum(
            len(self.call_binding(call).description.state_outputs)
            for call in self.calls
        )
        self.host = EmbeddedHost(
            tuple(
                Allocation(
                    name=allocation.name,
                    binding=self.bindings[allocation.implementation],
                    grants=resource_grants(
                        self.bindings[allocation.implementation]
                    ),
                )
                for allocation in fixture.allocations
            ),
            limits=HostLimits(
                analysis_records=len(fixture.analyses),
                operation_records=len(self.calls),
                input_bytes=max(1, sum(len(call.payload) for call in self.calls)),
                result_bytes=max(
                    limits.minimum_result_bytes,
                    *(result_allowance(call, limits) for call in self.calls),
                ),
                state_values=max(1, state_values),
                progress_records=max(
                    16,
                    *(
                        len(call.expected.progress)
                        for call in self.calls
                        if isinstance(call.expected, ExpectedSuccess)
                    ),
                ),
                initialization_seconds=limits.initialization_seconds,
                retirement_seconds=limits.retirement_seconds,
            ),
        )

    def binding(self, allocation_name: str) -> ImplementationBinding:
        allocation = self.allocations[allocation_name]
        return self.bindings[allocation.implementation]

    def call_binding(self, fixture: CallFixture):
        return next(
            call
            for call in self.binding(fixture.allocation).calls
            if call.description.name == fixture.call
        )

    async def run(self) -> None:
        self.case.addAsyncCleanup(
            retire_host,
            self.host,
            label=f"host retirement for {self.fixture.name}",
            seconds=self.limits.observation_seconds,
        )
        for analysis in self.fixture.analyses:
            await self.run_analysis(analysis)
        await settled(
            self.host.retire(),
            label=f"host retirement for {self.fixture.name}",
            seconds=self.limits.observation_seconds,
        )

    async def run_analysis(self, fixture: AnalysisFixture) -> None:
        self.case.assertEqual(
            len(fixture.allocations),
            len(set(fixture.allocations)),
            msg=f"Analysis {fixture.name!r} repeats an allocation",
        )
        analysis = self.host.open_analysis(
            f"{self.fixture.name}/{fixture.name}",
            allocation_names=fixture.allocations,
        )
        self.case.addAsyncCleanup(
            close_analysis,
            analysis,
            label=f"analysis cleanup for {fixture.name}",
            seconds=self.limits.observation_seconds,
        )
        saved_states = {}
        operations = {}

        for call_fixture in fixture.calls:
            self.case.assertIn(
                call_fixture.allocation,
                fixture.allocations,
                msg=f"Call {call_fixture.operation_id!r} uses an unreserved allocation",
            )
            binding = self.binding(call_fixture.allocation)
            call = self.call_binding(call_fixture)
            reservation = next(
                reservation
                for reservation in analysis.reservations
                if reservation.allocation == call_fixture.allocation
            )
            states = ()
            if call_fixture.states_from is not None:
                self.case.assertIn(
                    call_fixture.states_from,
                    saved_states,
                    msg=f"Call {call_fixture.operation_id!r} names unavailable state",
                )
                states = saved_states[call_fixture.states_from]

            operation = analysis.submit(
                Invocation(
                    operation_id=call_fixture.operation_id,
                    target=CallRef(
                        binding_id=binding.description.binding_id,
                        binding_revision=binding.description.binding_revision,
                        name=call_fixture.call,
                    ),
                    payload=call_fixture.payload,
                    states=states,
                    reservation=reservation,
                    limits=ExecutionLimits(
                        queue_seconds=self.limits.queue_seconds,
                        execution_seconds=self.limits.execution_seconds,
                        cleanup_seconds=self.limits.cleanup_seconds,
                        input_bytes=max(1, len(call_fixture.payload)),
                        result_bytes=result_allowance(call_fixture, self.limits),
                    ),
                ),
                dispatch_guard=allow,
            )
            self.case.assertNotIsInstance(
                operation,
                NotAdmitted,
                msg=(
                    f"Call {call_fixture.operation_id!r} was not admitted: "
                    f"{operation.reason if isinstance(operation, NotAdmitted) else ''}"
                ),
            )
            operations[call_fixture.operation_id] = operation
            observation = await finished(
                operation,
                seconds=self.limits.observation_seconds,
            )
            expected = call_fixture.expected

            if isinstance(expected, ExpectedScientificRejection):
                self.case.assertIs(Outcome.REJECTED, observation.outcome)
                self.case.assertEqual(1, len(observation.failures))
                rejection = observation.failures[0].cause
                self.case.assertIsInstance(rejection, ScientificRejection)
                self.case.assertEqual(expected.code, rejection.code)
                self.case.assertIs(ResultState.NONE, observation.result_state)
                continue

            self.case.assertIsInstance(expected, ExpectedSuccess)
            self.case.assertIs(Outcome.SUCCEEDED, observation.outcome)
            self.case.assertEqual((), observation.failures)
            self.case.assertIs(ResultState.AVAILABLE, observation.result_state)
            self.case.assertIsNotNone(observation.result)
            self.case.assertEqual(expected.progress, observation.progress)
            self.case.assertEqual(
                tuple(
                    (state.name, state.kind)
                    for state in call.description.state_outputs
                ),
                tuple(
                    (state.name, state.reference.kind)
                    for state in observation.states
                ),
            )
            self.case.assertEqual(
                expected.result,
                await operation.read_result(observation.result),
            )
            if expected.save_states_as is not None:
                self.case.assertNotIn(expected.save_states_as, saved_states)
                saved_states[expected.save_states_as] = observation.states
            await settled(
                operation.release_result(observation.result),
                label=f"result release for {call_fixture.operation_id}",
                seconds=self.limits.observation_seconds,
            )
            released = await operation.observe(after=None, wait_seconds=0)
            self.case.assertIs(ResultState.RELEASED, released.result_state)

        await settled(
            analysis.close(),
            label=f"analysis cleanup for {fixture.name}",
            seconds=self.limits.observation_seconds,
        )
        for operation in operations.values():
            observation = await operation.observe(after=None, wait_seconds=0)
            self.case.assertIs(ResourceState.RELEASED, observation.resources)


async def run_provider_fixture(
    case,
    fixture: ProviderFixture,
    limits: ConformanceLimits,
) -> None:
    await _Runner(case, fixture, limits).run()
