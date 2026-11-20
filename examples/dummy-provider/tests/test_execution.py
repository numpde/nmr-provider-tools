"""Check that the independently installed dummy binding runs through the host API."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import pathlib
import shutil
from tempfile import TemporaryDirectory
import unittest

import mock_numpy as np

from unittest.mock import patch

from dummy_provider import science
from dummy_provider import provider as provider_setup
from dummy_provider.binding import WeightedSum, WeightedSumConfig
from dummy_provider.provider import read_resource_requirements
from provider_core.contribution import ProgressEvent
from provider_core.embedded_host import InProcessExecutionHost

from provider_core.hosting import (
    OperationPolicy,
    NotAdmitted,
    Outcome,
    ResultConsumption,
    ResultState,
    observe_cleanup,
    observe_completion,
)


class DummyInstalledExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = pathlib.Path(self.enterContext(TemporaryDirectory()))
        configuration = directory / "provider.toml"

        shutil.copyfile(
            src=pathlib.Path(__file__).parent.parent / "config/deployments/localhost/provider.toml",
            dst=configuration,
        )

        self.enterContext(patch.object(target=provider_setup, attribute="PROVIDER_CONFIG_PATH", new=configuration))

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

        host = InProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )

        self.addAsyncCleanup(self.retire_host, host)

        hosted = host.adopt(
            contribution=WeightedSum(
                configuration=WeightedSumConfig(
                    settings=science.WeightedSumSettings(scale=2, offset=1),
                    coefficients=science.CoefficientSet(weights=np.array(object=[1, 2, 3], dtype=float)),
                )
            ),
            requirements=read_resource_requirements(),
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
            patch("dummy_provider.science.prepare", wraps=science.prepare) as prepare_samples_mock,
        ):
            analysis = host.open_analysis(allocations=frozenset({hosted.allocation}))
            (reservation,) = analysis.reservations

            operation = analysis.submit_method(
                method=contribution.prepare,
                scientific_input=science.PreparationInput(samples=np.array(object=[1, 2, 3], dtype=float)),
                states={},
                reservation=reservation,
                policy=OperationPolicy(
                    queue_stop_request_seconds=5,
                    execution_stop_request_seconds=5,
                    cleanup_overdue_flag_seconds=5,
                    reject_encoded_input_over_bytes=4096,
                    reject_encoded_result_over_bytes=4096,
                ),
            )

            async with asyncio.timeout(10):
                observation = await observe_completion(operation=operation)

                self.assertTrue(any((observed_failure.cause is failure) for observed_failure in observation.failures))
                prepare_samples_mock.assert_not_called()
                (model,) = loaded_models

                self.assertIs(model, contribution.model)
                self.assertIsNotNone(model.loaded_coefficients)
                await observe_cleanup(control=analysis.close())
                await observe_cleanup(control=host.retire())

            dispose.assert_called_once_with(model=model)
            self.assertIsNone(model.loaded_coefficients)

    async def test_installed_binding_prepares_and_calculates(self):
        host_options = dict(
            input_bytes=4096,
            result_bytes=4096,
            state_values=2,
            progress_records=8,
            initialization_seconds=5,
            retirement_seconds=5,
        )

        host = InProcessExecutionHost(
            **host_options,
        )

        self.addAsyncCleanup(self.retire_host, host)

        weighted_binding = host.adopt(
            contribution=WeightedSum(
                configuration=WeightedSumConfig(
                    settings=science.WeightedSumSettings(scale=2, offset=1),
                    coefficients=science.CoefficientSet(weights=np.array(object=[1, 2, 3], dtype=float)),
                )
            ),
            requirements=read_resource_requirements(),
        )

        analysis = host.open_analysis(
            allocations=frozenset({weighted_binding.allocation}),
        )

        (reservation,) = analysis.reservations
        (prepare_call, calculate_call) = (weighted_binding.call(method=WeightedSum.prepare), weighted_binding.call(method=WeightedSum.calculate))

        operation_policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=10,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=1024,
            reject_encoded_result_over_bytes=1024,
        )

        preparation_input = science.PreparationInput(samples=np.array(object=[1, 2, 3], dtype=float))

        rejected = analysis.submit_method(
            method=weighted_binding.contribution.prepare,
            scientific_input=science.PreparationInput(samples=np.array(object=[], dtype=float)),
            states={},
            reservation=reservation,
            policy=operation_policy,
        )

        rejection = await observe_completion(operation=rejected)

        self.assertIs(Outcome.REJECTED, rejection.outcome)
        original_session = analysis.science[weighted_binding.allocation.allocation].analysis_instance

        self.assertTrue(original_session.scientific_session.is_open)

        preparation_operation = analysis.submit_method(
            method=weighted_binding.contribution.prepare,
            scientific_input=preparation_input,
            states={},
            reservation=reservation,
            policy=operation_policy,
        )

        self.assertNotIsInstance(preparation_operation, NotAdmitted)

        preparation_observation = await self.assert_succeeded(preparation_operation)

        self.assertIs(original_session, analysis.science[weighted_binding.allocation.allocation].analysis_instance)
        self.assertEqual(1, len(preparation_observation.states))
        self.assertEqual("normalized", preparation_observation.states[0].name)

        prepared = await WeightedSum.normalized.reference(
            call=prepare_call,
            operation=preparation_operation,
        )

        self.assertEqual("candidate", WeightedSum.candidate.name)
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
            states={WeightedSum.candidate: prepared},
            reservation=reservation,
            policy=operation_policy,
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
