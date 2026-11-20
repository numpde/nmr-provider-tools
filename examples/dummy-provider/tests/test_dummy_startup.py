"""Run unchanged caller sequencing through both installed startup selections."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mock_numpy as np

from provider_core.hosting import NotAdmitted, Outcome, ResultConsumption, settle_cleanup
from provider_core.runner_session import RunnerSession
from provider_core.submission import Submission
from tests.fakes.runner import make_scientific_session

from dummy_provider import provider as provider_setup
from dummy_provider import science
from dummy_provider.binding import WeightedSum


class DummyStartupTests(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, provider):
        """Reject and correct one input, then consume its state in the same analysis."""

        host = provider.execution_host
        weighted = provider.weighted
        preparation = weighted.call(method=WeightedSum.prepare)
        calculation = weighted.call(method=WeightedSum.calculate)
        analysis = host.open_analysis(allocations=frozenset({weighted.allocation}))
        reservation = analysis.reservation(allocation=weighted.allocation)

        try:
            rejected = Submission.from_call(
                analysis_executor=analysis,
                call_binding=preparation,
                scientific_input=science.PreparationInput(samples=np.array(object=[], dtype=float)),
                states={},
                reservation_ref=reservation,
                operation_policy=provider.operation_policy,
            )
            self.assertNotIsInstance(rejected.submit(), NotAdmitted)
            self.assertIs((await rejected.wait()).outcome, Outcome.REJECTED)

            prepared = Submission.from_call(
                analysis_executor=analysis,
                call_binding=preparation,
                scientific_input=science.PreparationInput(samples=np.array(object=[1, 2, 3], dtype=float)),
                states={},
                reservation_ref=reservation,
                operation_policy=provider.operation_policy,
            )
            operation = prepared.submit()
            self.assertNotIsInstance(operation, NotAdmitted)
            observation = await prepared.wait()
            self.assertIs(observation.outcome, Outcome.SUCCEEDED)
            self.assertEqual((), observation.failures)
            self.assertIs(operation, prepared.submit())
            state = await WeightedSum.normalized.reference(call=preparation, operation=operation)

            async def receive_preparation(payload):
                self.assertEqual(science.PreparationResult(count=3), preparation.result_codec.decode(payload=payload))

            consumption = ResultConsumption(operation=operation, result=observation.result, receive=receive_preparation)
            self.addAsyncCleanup(consumption.settle)
            await consumption.consume()

            calculated = Submission.from_call(
                analysis_executor=analysis,
                call_binding=calculation,
                states={WeightedSum.candidate: state},
                reservation_ref=reservation,
                operation_policy=provider.operation_policy,
            )
            operation = calculated.submit()
            self.assertNotIsInstance(operation, NotAdmitted)
            observation = await calculated.wait()
            self.assertIs(observation.outcome, Outcome.SUCCEEDED)
            self.assertEqual((), observation.failures)
            self.assertEqual((0, 1), tuple(event.completed for event in observation.progress))
            self.assertIs(operation, calculated.submit())

            async def receive_calculation(payload):
                self.assertEqual(science.PredictionResult(total=29, count=3), calculation.result_codec.decode(payload=payload))

            consumption = ResultConsumption(operation=operation, result=observation.result, receive=receive_calculation)
            self.addAsyncCleanup(consumption.settle)
            await consumption.consume()
        finally:
            await settle_cleanup(control=analysis.close())

    async def test_thread_and_process_startup_keep_the_same_request_sequence(self):
        configuration_text = (Path(__file__).parent.parent / "config/deployments/localhost/provider.toml").read_text(encoding="utf-8")

        for backend in ("thread", "process"):
            with self.subTest(backend=backend), TemporaryDirectory() as temporary:
                configuration = Path(temporary) / "provider.toml"
                configuration.write_text(configuration_text.replace('backend = "thread"', f'backend = "{backend}"'), encoding="utf-8")
                session = None
                runner = None

                if backend == "process":
                    runner, session = make_scientific_session(self)

                with patch.object(provider_setup, "PROVIDER_CONFIG_PATH", configuration):
                    async with asyncio.timeout(20):
                        async with provider_setup.open_provider(process_session=session) as provider:
                            if backend == "thread":
                                model = provider.weighted.contribution.model
                            else:
                                process_id = provider.execution_host.process_id

                            await self.run_request(provider)
                            await self.run_request(provider)

                            if backend == "thread":
                                self.assertIs(model, provider.weighted.contribution.model)
                                self.assertIsNotNone(model.loaded_coefficients)
                            else:
                                self.assertEqual(process_id, provider.execution_host.process_id)

                        if backend == "thread":
                            self.assertIsNone(model.loaded_coefficients)
                        else:
                            self.assertTrue(session.retirement.scratch_released)
                            self.assertEqual([], list(runner.scratch.iterdir()))

    async def test_configuration_refusals_leave_the_unconnected_session_with_its_caller(self):
        configuration_text = (Path(__file__).parent.parent / "config/deployments/localhost/provider.toml").read_text(encoding="utf-8")

        for backend in ("thread", "unknown"):
            with self.subTest(backend=backend), TemporaryDirectory() as temporary:
                configuration = Path(temporary) / "provider.toml"
                configuration.write_text(configuration_text.replace('backend = "thread"', f'backend = "{backend}"'), encoding="utf-8")
                session = RunnerSession(Path(temporary) / "unused.sock", role="science", expected_lock_identity=(0, 0), startup_seconds=5, retirement_seconds=5)
                self.addCleanup(session.close)
                original_descriptor = session.control.fileno()

                with patch.object(provider_setup, "PROVIDER_CONFIG_PATH", configuration):
                    with self.assertRaises(ValueError):
                        async with provider_setup.open_provider(process_session=session):
                            self.fail("Refused configuration yielded a provider")

                self.assertFalse(session.control_connected)
                self.assertEqual(original_descriptor, session.control.fileno())
