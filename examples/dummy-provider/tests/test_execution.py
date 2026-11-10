"""Exercise the installed dummy binding through provider-core's public host API."""

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
    async def finished(self, operation):
        async with asyncio.timeout(10):
            observation = await operation.observe(after=None, wait_seconds=0)
            while not observation.finished:
                observation = await operation.observe(
                    after=observation.revision,
                    wait_seconds=1,
                )
        self.assertIs(Outcome.SUCCEEDED, observation.outcome)
        self.assertEqual((), observation.failures)
        return observation

    async def settled(self, control):
        async with asyncio.timeout(10):
            observation = await control.observe(after=None, wait_seconds=0)
            while not observation.settled and not observation.failures:
                observation = await control.observe(
                    after=observation.revision,
                    wait_seconds=1,
                )
        self.assertEqual((), observation.failures)
        self.assertTrue(observation.settled)

    async def retire_host(self, host):
        await self.settled(host.retire())

    async def test_installed_binding_prepares_and_calculates(self):
        binding = create_weighted_sum_binding(
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )
        host = EmbeddedHost(
            (
                Allocation(
                    name="weighted",
                    binding=binding,
                    grants=(
                        ResourceGrant(
                            kind="cpu",
                            amount=1,
                            unit="thread",
                            selector="generic",
                        ),
                    ),
                ),
            ),
            limits=HostLimits(
                analysis_records=1,
                operation_records=2,
                input_bytes=4096,
                result_bytes=4096,
                state_values=2,
                progress_records=8,
                initialization_seconds=5,
                retirement_seconds=5,
            ),
        )
        self.addAsyncCleanup(self.retire_host, host)

        analysis = host.open_analysis(
            "weighted-analysis",
            allocation_names=("weighted",),
        )
        reservation, = analysis.reservations
        prepare_call, calculate_call = binding.calls
        limits = ExecutionLimits(
            queue_seconds=5,
            execution_seconds=10,
            cleanup_seconds=5,
            input_bytes=1024,
            result_bytes=1024,
        )

        def submit(call, *, operation_id, payload, states=()):
            return analysis.submit(
                Invocation(
                    operation_id=operation_id,
                    target=CallRef(
                        binding_id=binding.description.binding_id,
                        binding_revision=binding.description.binding_revision,
                        name=call.description.name,
                    ),
                    payload=payload,
                    states=states,
                    reservation=reservation,
                    limits=limits,
                ),
                dispatch_guard=lambda: None,
            )

        preparation = submit(
            prepare_call,
            operation_id="prepare",
            payload=prepare_call.input_codec.encode(
                science.PreparationInput(values=(2, 4, 6)),
            ),
        )
        self.assertNotIsInstance(preparation, NotAdmitted)
        prepared = await self.finished(preparation)
        self.assertEqual(1, len(prepared.states))
        self.assertIs(ResultState.AVAILABLE, prepared.result_state)
        self.assertIsNotNone(prepared.result)
        await self.settled(preparation.release_result(prepared.result))

        calculation = submit(
            calculate_call,
            operation_id="calculate",
            payload=calculate_call.input_codec.encode(NoInput()),
            states=prepared.states,
        )
        self.assertNotIsInstance(calculation, NotAdmitted)
        calculated = await self.finished(calculation)
        self.assertIs(ResultState.AVAILABLE, calculated.result_state)
        self.assertIsNotNone(calculated.result)
        result_bytes = await calculation.read_result(calculated.result)
        self.assertEqual(
            science.PredictionResult(total=57, count=3),
            calculate_call.result_codec.decode(result_bytes),
        )
        self.assertEqual(
            (
                ProgressEvent(stage="calculation", completed=0, total=1),
                ProgressEvent(stage="calculation", completed=1, total=1),
            ),
            calculated.progress,
        )
        await self.settled(calculation.release_result(calculated.result))
        await self.settled(analysis.close())


if __name__ == "__main__":
    unittest.main()
