"""Check that the independently installed dummy binding runs through the host API."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import unittest

from dummy_provider import science
from dummy_provider.binding import create_weighted_sum_binding
from provider_core.contribution import CallRef, NoInput, ProgressEvent
from provider_core.embedded_host import Allocation, EmbeddedHost, HostLimits
from provider_core.hosting import (
    ExecutionLimits,
    Invocation,
    NotAdmitted,
    Outcome,
    ResourceGrant,
    ResultState,
)


class DummyInstalledExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def assert_succeeded(self, operation):
        """Wait for the operation's terminal observation and require success."""
        async with asyncio.timeout(10):
            observed_operation = await operation.observe(after=None, wait_seconds=0)
            while not observed_operation.finished:
                observed_operation = await operation.observe(
                    after=observed_operation.revision,
                    wait_seconds=1,
                )

        self.assertIs(Outcome.SUCCEEDED, observed_operation.outcome)
        self.assertEqual((), observed_operation.failures)
        return observed_operation

    async def assert_settled(self, control):
        """Observe required cleanup before reporting it as complete."""
        async with asyncio.timeout(10):
            observed_cleanup = await control.observe(after=None, wait_seconds=0)
            while not observed_cleanup.settled and not observed_cleanup.failures:
                observed_cleanup = await control.observe(
                    after=observed_cleanup.revision,
                    wait_seconds=1,
                )

        self.assertEqual((), observed_cleanup.failures)
        self.assertTrue(observed_cleanup.settled)

    async def retire_host(self, host):
        await self.assert_settled(host.retire())

    async def test_installed_binding_prepares_and_calculates(self):
        weighted_binding = create_weighted_sum_binding(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )
        model_allocation = Allocation(
            name="weighted",
            binding=weighted_binding,
            grants=(
                ResourceGrant(
                    kind="cpu",
                    amount=1,
                    unit="thread",
                    selector="generic",
                ),
            ),
        )
        host_limits = HostLimits(
            analysis_records=1,
            operation_records=2,
            input_bytes=4096,
            result_bytes=4096,
            state_values=2,
            progress_records=8,
            initialization_seconds=5,
            retirement_seconds=5,
        )
        host = EmbeddedHost((model_allocation,), limits=host_limits)
        self.addAsyncCleanup(self.retire_host, host)

        analysis = host.open_analysis(
            "weighted-analysis",
            allocation_names=("weighted",),
        )
        (reservation,) = analysis.reservations
        (prepare_call, calculate_call) = weighted_binding.calls

        execution_limits = ExecutionLimits(
            queue_seconds=5,
            execution_seconds=10,
            cleanup_seconds=5,
            input_bytes=1024,
            result_bytes=1024,
        )

        def submit_call(*, scientific_call, operation_id: str, payload: bytes, states):
            return analysis.submit(
                Invocation(
                    operation_id=operation_id,
                    target=CallRef(
                        binding_id=weighted_binding.description.binding_id,
                        binding_revision=weighted_binding.description.binding_revision,
                        name=scientific_call.description.name,
                    ),
                    payload=payload,
                    states=states,
                    reservation=reservation,
                    limits=execution_limits,
                ),
                dispatch_guard=lambda: None,
            )

        preparation_input = science.PreparationInput(values=(1, 2, 3))

        preparation_operation = submit_call(
            scientific_call=prepare_call,
            operation_id="prepare",
            payload=prepare_call.input_codec.encode(preparation_input),
            states=(),
        )
        self.assertNotIsInstance(preparation_operation, NotAdmitted)

        preparation_observation = await self.assert_succeeded(preparation_operation)
        self.assertEqual(1, len(preparation_observation.states))
        self.assertIs(ResultState.AVAILABLE, preparation_observation.result_state)
        self.assertIsNotNone(preparation_observation.result)

        preparation_result = prepare_call.result_codec.decode(
            payload=(await preparation_operation.read_result(preparation_observation.result)),
        )
        self.assertEqual(science.PreparationResult(count=3), preparation_result)
        await self.assert_settled(
            preparation_operation.release_result(preparation_observation.result)
        )

        # Result release leaves the returned scientific state available to calculate.
        calculation_operation = submit_call(
            scientific_call=calculate_call,
            operation_id="calculate",
            payload=calculate_call.input_codec.encode(NoInput()),
            states=preparation_observation.states,
        )
        self.assertNotIsInstance(calculation_operation, NotAdmitted)

        calculation_observation = await self.assert_succeeded(calculation_operation)
        self.assertIs(ResultState.AVAILABLE, calculation_observation.result_state)
        self.assertIsNotNone(calculation_observation.result)

        calculation_result = calculate_call.result_codec.decode(
            payload=(await calculation_operation.read_result(calculation_observation.result)),
        )

        self.assertEqual(science.PredictionResult(total=29, count=3), calculation_result)
        self.assertEqual(
            (
                ProgressEvent(stage="calculation", completed=0, total=1),
                ProgressEvent(stage="calculation", completed=1, total=1),
            ),
            calculation_observation.progress,
        )
        await self.assert_settled(
            calculation_operation.release_result(calculation_observation.result)
        )
        await self.assert_settled(analysis.close())


if __name__ == "__main__":
    unittest.main()
