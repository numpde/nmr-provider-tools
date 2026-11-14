"""Check that the independently installed dummy binding runs through the host API."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import unittest
from unittest.mock import patch

from dummy_provider import science
from dummy_provider.binding import WeightedSum, create_weighted_sum_binding
from provider_core.contribution import ProgressEvent
from provider_core.embedded_host import EmbeddedHost, HostLimits, ResourceAllocation
from provider_core.hosting import (
    ExecutionLimits,
    NotAdmitted,
    Outcome,
    ResourceGrant,
    ResultConsumption,
    ResultState,
    observe_cleanup,
    observe_completion,
)


class DummyInstalledExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def assert_succeeded(self, operation):
        """Wait for the operation's terminal observation and require success."""
        async with asyncio.timeout(10):
            observed_operation = await observe_completion(operation)

        self.assertIs(Outcome.SUCCEEDED, observed_operation.outcome)
        self.assertEqual((), observed_operation.failures)
        return observed_operation

    async def assert_settled(self, control):
        """Observe required cleanup before reporting it as complete."""
        async with asyncio.timeout(10):
            observed_cleanup = await observe_cleanup(control)

        self.assertEqual((), observed_cleanup.failures)
        self.assertTrue(observed_cleanup.settled)

    async def retire_host(self, host):
        await self.assert_settled(host.retire())

    async def test_partial_model_loading_keeps_the_original_cleanup_owner(self):
        """A loader may fail after acquisition; retirement still owns that model."""
        host = EmbeddedHost(limits=HostLimits(
            analysis_records=1, operation_records=1, input_bytes=4096, result_bytes=4096,
            state_values=1, progress_records=1, initialization_seconds=5, retirement_seconds=5,
        ))
        self.addAsyncCleanup(self.retire_host, host)
        hosted = create_weighted_sum_binding(
            host=host,
            allocation=ResourceAllocation(name="weighted", grants=(
                ResourceGrant(kind="cpu", amount=1, unit="thread", selector="generic"),
            )),
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )
        contribution = hosted.contribution
        failure = OSError("Cannot finish loading model coefficients")
        loaded_models = []

        # Keep the real loader before patching the provider's module reference.
        load_model = science.load_model

        def partially_load(*, model):
            load_model(model=model)
            loaded_models.append(model)
            raise failure

        with (
            patch("dummy_provider.science.load_model", side_effect=partially_load),
            patch("dummy_provider.science.dispose_model", wraps=science.dispose_model) as dispose,
            patch("dummy_provider.science.prepare", wraps=science.prepare) as prepare,
        ):
            analysis = host.open_analysis(analysis_id="failed-load", allocation_names=("weighted",))
            reservation, = analysis.reservations
            operation = analysis.submit_method(
                method=contribution.prepare, operation_id="prepare",
                scientific_input=science.PreparationInput(values=(1, 2, 3)),
                states=(), reservation=reservation,
                limits=ExecutionLimits(
                    queue_seconds=5, execution_seconds=5, cleanup_seconds=5,
                    input_bytes=4096, result_bytes=4096,
                ),
                dispatch_guard=lambda: None,
            )
            async with asyncio.timeout(10):
                observation = await observe_completion(operation=operation)
                self.assertTrue(any(item.cause is failure for item in observation.failures))
                prepare.assert_not_called()
                model, = loaded_models
                self.assertIs(model, contribution.model)
                self.assertIsNotNone(model.loaded_coefficients)
                await observe_cleanup(control=analysis.close())
                await observe_cleanup(control=host.retire())
            dispose.assert_called_once_with(model=model)
            self.assertIsNone(model.loaded_coefficients)

    async def test_installed_binding_prepares_and_calculates(self):
        model_allocation = ResourceAllocation(
            name="weighted",
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
            operation_records=3,
            input_bytes=4096,
            result_bytes=4096,
            state_values=2,
            progress_records=8,
            initialization_seconds=5,
            retirement_seconds=5,
        )
        host = EmbeddedHost(limits=host_limits)
        self.addAsyncCleanup(self.retire_host, host)
        weighted_binding = create_weighted_sum_binding(
            host=host, allocation=model_allocation,
            settings=science.WeightedSumSettings(scale=2, offset=1),
            coefficients=science.CoefficientSet(values=(1, 2, 3)),
        )

        analysis = host.open_analysis(
            analysis_id="weighted-analysis",
            allocation_names=("weighted",),
        )
        (reservation,) = analysis.reservations
        prepare_call, calculate_call = weighted_binding.call(method=WeightedSum.prepare), weighted_binding.call(method=WeightedSum.calculate)

        execution_limits = ExecutionLimits(
            queue_seconds=5,
            execution_seconds=10,
            cleanup_seconds=5,
            input_bytes=1024,
            result_bytes=1024,
        )

        preparation_input = science.PreparationInput(values=(1, 2, 3))

        rejected = analysis.submit_method(
            method=weighted_binding.contribution.prepare,
            operation_id="rejected-prepare", scientific_input=science.PreparationInput(values=()),
            states=(), reservation=reservation, limits=execution_limits,
            dispatch_guard=lambda: None,
        )
        rejection = await observe_completion(operation=rejected)
        self.assertIs(Outcome.REJECTED, rejection.outcome)
        original_session = analysis.science["weighted"].value
        self.assertTrue(original_session.value.is_open)

        preparation_operation = analysis.submit_method(
            method=weighted_binding.contribution.prepare,
            operation_id="prepare",
            scientific_input=preparation_input,
            states=(),
            reservation=reservation,
            limits=execution_limits,
            dispatch_guard=lambda: None,
        )
        self.assertNotIsInstance(preparation_operation, NotAdmitted)

        preparation_observation = await self.assert_succeeded(preparation_operation)
        self.assertIs(original_session, analysis.science["weighted"].value)
        self.assertEqual(1, len(preparation_observation.states))
        self.assertEqual("normalized", preparation_observation.states[0].name)
        prepared = await WeightedSum.prepared_output.reference(
            call=prepare_call, operation=preparation_operation,
        )
        calculation_state = WeightedSum.prepared_input.bind(handle=prepared)
        self.assertEqual("candidate", calculation_state.name)
        self.assertIs(ResultState.AVAILABLE, preparation_observation.result_state)
        self.assertIsNotNone(preparation_observation.result)

        async def accept_preparation(payload: bytes) -> None:
            preparation_result = prepare_call.result_codec.decode(payload=payload)
            self.assertEqual(science.PreparationResult(count=3), preparation_result)

        preparation_consumption = ResultConsumption(
            operation=preparation_operation,
            result=preparation_observation.result,
            receive=accept_preparation,
        )
        self.addAsyncCleanup(preparation_consumption.settle)
        async with asyncio.timeout(10):
            await preparation_consumption.consume()

        # Result release leaves the returned scientific state available to calculate.
        calculation_operation = analysis.submit_method(
            method=weighted_binding.contribution.calculate,
            operation_id="calculate",
            states=(calculation_state,),
            reservation=reservation,
            limits=execution_limits,
            dispatch_guard=lambda: None,
        )
        self.assertNotIsInstance(calculation_operation, NotAdmitted)

        calculation_observation = await self.assert_succeeded(calculation_operation)
        self.assertIs(ResultState.AVAILABLE, calculation_observation.result_state)
        self.assertIsNotNone(calculation_observation.result)

        async def accept_calculation(payload: bytes) -> None:
            calculation_result = calculate_call.result_codec.decode(payload=payload)
            self.assertEqual(science.PredictionResult(total=29, count=3), calculation_result)

        self.assertEqual(
            (
                ProgressEvent(stage="calculation", completed=0, total=1),
                ProgressEvent(stage="calculation", completed=1, total=1),
            ),
            calculation_observation.progress,
        )
        calculation_consumption = ResultConsumption(
            operation=calculation_operation,
            result=calculation_observation.result,
            receive=accept_calculation,
        )
        self.addAsyncCleanup(calculation_consumption.settle)
        async with asyncio.timeout(10):
            await calculation_consumption.consume()
        await self.assert_settled(analysis.close())


if __name__ == "__main__":
    unittest.main()
