"""Exercise the complete composition with real SDK owners and small stand-ins."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import replace
from importlib import metadata, resources
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tomllib
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import OperationPolicy, NotAdmitted, StartDeadlineExpired, ResultConsumption, ResultState, observe_cleanup, observe_completion
from provider_core.interpreter import Interpreter, ModelWindowExpired, ProseService
from provider_core.scientific import ResourceRequirement

from nmrpeak_mock_provider.application import Prediction
from nmrpeak_mock_provider.binding import Chf, Hf, PeakContribution
from nmrpeak_mock_provider.composition import adopt_offerings, interpretation_messages, interpretation_tools
from nmrpeak_mock_provider.fake_model import Model, Tokenizer
from nmrpeak_mock_provider.fake_services import RecordingReceiver, ScriptedEndpointFactory
from nmrpeak_mock_provider.science import Settings


def candidate(*, carbon):
    document = {"formula": "C2H6O", "proton_peaks": [{
        "centroid": "1.2", "integral": 3, "multiplicity": "triplet", "couplings": ["7"],
    }]}
    if carbon:
        document["carbon_peaks"] = ["18", "58"]
    return document


class PeakExampleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = EmbeddedHost(
            input_bytes=65536,
            result_bytes=65536,
            state_values=8,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )
        self.addAsyncCleanup(self.retire)
        with (Path(__file__).parent / "config/deployments/localhost/provider.toml").open(mode="rb") as source:
            provider_config = tomllib.load(source)
        self.offerings = adopt_offerings(
            host=self.host,
            requested_requirements=frozenset({ResourceRequirement(kind="cpu", amount=1, unit="thread", selector="generic")}),
            hf_settings=Settings(**provider_config["science"]["hf"]["settings"]),
            chf_settings=Settings(**provider_config["science"]["chf"]["settings"]),
            create_model=Model,
        )
        self.hf, = [hosted for hosted in self.offerings if isinstance(hosted.contribution, Hf)]
        self.chf, = [hosted for hosted in self.offerings if isinstance(hosted.contribution, Chf)]

    async def retire(self):
        await observe_cleanup(control=self.host.retire())

    async def close_analysis(self, *, execution):
        await observe_cleanup(control=execution.close())

    def prediction(self, *, hosted, proposals, name):
        factory = ScriptedEndpointFactory(proposals=proposals, context_bytes=32768)
        interpreter = Interpreter(
            endpoint_factories=(ProseService(factory=factory),), timeout_seconds=15,
            context_bytes=factory.context_bytes, secrets=(),
        )
        self.addAsyncCleanup(interpreter.retire)
        execution = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        self.addAsyncCleanup(self.close_analysis, execution=execution)
        return Prediction(
            execution=execution, hosted=hosted, interpreter=interpreter,
            policy=OperationPolicy(
                queue_stop_request_seconds=5,
                execution_stop_request_seconds=5,
                cleanup_overdue_flag_seconds=5,
                reject_encoded_input_over_bytes=8192,
                reject_encoded_result_over_bytes=8192,
            ),
        ), factory

    async def interpret(self, *, prediction, carbon):
        async with asyncio.timeout(15):
            await prediction.interpret(messages=interpretation_messages(source="Recorded NMR source"), tools=interpretation_tools(carbon=carbon))

    async def test_both_offerings_interpret_validate_generate_and_deliver(self):
        for hosted in self.offerings:
            carbon = isinstance(hosted.contribution, Chf)
            with self.subTest(carbon=carbon):
                prediction, factory = self.prediction(hosted=hosted, proposals=(candidate(carbon=carbon),), name=hosted.allocation.allocation)
                await self.interpret(prediction=prediction, carbon=carbon)
                receiver = RecordingReceiver()
                result = await prediction.finish()
                consumption = ResultConsumption(operation=prediction.generation, result=result, receive=receiver)
                await consumption.consume()
                await consumption.settle()
                report, = receiver.payloads
                decoded = json.loads(s=report)
                self.assertEqual(first=2, second=len(decoded["generated_smiles"]))
                self.assertEqual(first=hosted.contribution.configuration.checkpoint_sha256, second=decoded["checkpoint_sha256"])
                self.assertTrue(expr=factory.endpoints[0].retirement_confirmed)
                observed = await prediction.generation.observe(after=None, wait_seconds=0)
                self.assertIs(expr1=ResultState.RELEASED, expr2=observed.result_state)
                await observe_cleanup(control=prediction.execution.close())

    async def test_invalid_interpretation_is_corrected_before_generation(self):
        bad = candidate(carbon=False)
        bad["proton_peaks"] = []
        prediction, factory = self.prediction(hosted=self.hf, proposals=(bad, candidate(carbon=False)), name="correction")
        await self.interpret(prediction=prediction, carbon=False)
        self.assertEqual(first=2, second=len(prediction.preparations))
        self.assertIsNone(obj=prediction.generation)
        self.assertEqual(first=2, second=len(factory.endpoints[0].requests))
        await prediction.finish()

    async def test_expired_interpretation_window_admits_no_scientific_work(self):
        prediction, _ = self.prediction(hosted=self.hf, proposals=(candidate(carbon=False),), name="expired")
        with self.assertRaises(expected_exception=ModelWindowExpired) as raised:
            await prediction.dispatch(
                name="submit_interpretation", arguments={"value": candidate(carbon=False)},
                not_after=asyncio.get_running_loop().time() - 1,
            )
        self.assertIsInstance(obj=raised.exception.__cause__, cls=StartDeadlineExpired)
        self.assertEqual(first=[], second=prediction.preparations)
        self.assertIsNone(obj=self.hf.contribution.model)

    async def test_interpreter_deadline_waits_for_initialization_to_settle(self):
        entered = Event()
        release = Event()
        load = Model.load

        def held_load(model, *, checkpoint):
            entered.set()
            release.wait(timeout=5)
            return load(model, checkpoint=checkpoint)

        prediction, _ = self.prediction(hosted=self.hf, proposals=(candidate(carbon=False),), name="loading")
        deadline = asyncio.get_running_loop().time() + 1
        with patch.object(target=Model, attribute="load", new=held_load):
            original = asyncio.create_task(coro=prediction.dispatch(
                name="submit_interpretation",
                arguments={"value": candidate(carbon=False)},
                not_after=deadline,
            ))
            try:
                self.assertTrue(expr=await asyncio.to_thread(entered.wait, 2))
                await asyncio.sleep(delay=max(0, deadline - asyncio.get_running_loop().time()) + 0.01)
                self.assertFalse(expr=original.done())
            finally:
                release.set()
            with self.assertRaises(expected_exception=ModelWindowExpired) as raised:
                await original
        self.assertIsInstance(obj=raised.exception.__cause__, cls=StartDeadlineExpired)
        self.assertEqual(first=1, second=len(prediction.preparations))
        observed = await prediction.preparations[0].observe(after=None, wait_seconds=0)
        self.assertTrue(expr=observed.finished)
        self.assertIsNone(obj=prediction.prepared)
        self.assertIsNotNone(obj=self.hf.contribution.model.tokenizer)

    async def test_hf_input_does_not_silently_accept_carbon_fields(self):
        prediction, factory = self.prediction(
            hosted=self.hf, proposals=(candidate(carbon=True), candidate(carbon=False)), name="wrong-offering",
        )
        await self.interpret(prediction=prediction, carbon=False)
        self.assertEqual(first=1, second=len(prediction.preparations))
        self.assertEqual(first=2, second=len(factory.endpoints[0].requests))

    async def test_warm_model_is_reused_by_sequential_analyses(self):
        models = []
        for name in ("first", "second"):
            prediction, _ = self.prediction(hosted=self.hf, proposals=(candidate(carbon=False),), name=name)
            await self.interpret(prediction=prediction, carbon=False)
            await prediction.finish()
            models.append(self.hf.contribution.model)
            await observe_cleanup(control=prediction.execution.close())
        self.assertIs(expr1=models[0], expr2=models[1])

    async def test_generation_consumes_candidate_once(self):
        prediction, _ = self.prediction(hosted=self.hf, proposals=(candidate(carbon=False),), name="consume")
        await self.interpret(prediction=prediction, carbon=False)
        original = await prediction.finish()
        self.assertEqual(first=original, second=await prediction.finish())
        reservation, = prediction.execution.reservations
        duplicate = prediction.execution.submit_method(
            method=self.hf.contribution.generate,
            states=(PeakContribution.candidate.bind(handle=prediction.prepared),),
            reservation=reservation, policy=prediction.policy,
        )
        self.assertIsInstance(obj=duplicate, cls=NotAdmitted)

    async def test_failed_receiver_keeps_report_without_regenerating(self):
        prediction, _ = self.prediction(hosted=self.hf, proposals=(candidate(carbon=False),), name="delivery")
        await self.interpret(prediction=prediction, carbon=False)
        cause = OSError("Receiver did not accept the report")
        attempts = []

        async def refuse(payload):
            attempts.append(payload)
            raise cause

        result = await prediction.finish()
        consumption = ResultConsumption(operation=prediction.generation, result=result, receive=refuse)
        original = prediction.generation
        for _ in range(2):
            with self.assertRaises(expected_exception=OSError) as raised:
                await consumption.consume()
            self.assertIs(expr1=cause, expr2=raised.exception)
        with self.assertRaises(expected_exception=OSError):
            await consumption.settle()
        completed = await observe_completion(operation=original)
        self.assertIs(expr1=ResultState.AVAILABLE, expr2=completed.result_state)
        self.assertIs(expr1=original, expr2=prediction.generation)
        self.assertEqual(first=1, second=len(attempts))

    async def test_cancelled_delivery_wait_joins_the_same_receiver(self):
        prediction, _ = self.prediction(hosted=self.hf, proposals=(candidate(carbon=False),), name="cancelled-delivery")
        await self.interpret(prediction=prediction, carbon=False)
        result = await prediction.finish()
        entered = asyncio.Event()
        accepted = asyncio.Event()
        deliveries = []

        async def receive(payload):
            deliveries.append(payload)
            entered.set()
            await accepted.wait()

        consumption = ResultConsumption(operation=prediction.generation, result=result, receive=receive)
        waiter = asyncio.create_task(coro=consumption.consume())
        try:
            async with asyncio.timeout(10):
                await entered.wait()
                waiter.cancel()
                with self.assertRaises(expected_exception=asyncio.CancelledError):
                    await waiter
                accepted.set()
                await consumption.consume()
        finally:
            accepted.set()
            await consumption.settle()
        self.assertEqual(first=1, second=len(deliveries))

    async def test_invalid_generated_candidates_fail_without_reinterpretation(self):
        for index, output in enumerate(((), ("",), ("C", "O", "CO"), ("C" * 65,))):
            with self.subTest(output=output):
                prediction, factory = self.prediction(hosted=self.hf, proposals=(candidate(carbon=False),), name=str(index))
                await self.interpret(prediction=prediction, carbon=False)
                with patch.object(target=Model, attribute="generate", return_value=output) as generate:
                    with self.assertRaises(expected_exception=ValueError) as raised:
                        await prediction.finish()
                    with self.assertRaises(expected_exception=ValueError) as repeated:
                        await prediction.finish()
                self.assertIs(expr1=raised.exception, expr2=repeated.exception)
                self.assertEqual(first=1, second=generate.call_count)
                self.assertEqual(first=1, second=len(factory.endpoints[0].requests))
                await observe_cleanup(control=prediction.execution.close())

    async def test_nonpositive_model_limits_are_rejected_before_loading(self):
        for field in ("beam_size", "maximum_input_tokens", "maximum_candidate_characters"):
            with self.subTest(field=field):
                with self.assertRaises(expected_exception=ValueError):
                    replace(self.hf.contribution.configuration, **{field: 0})
        self.assertIsNone(obj=self.hf.contribution.model)

    async def test_checkpoint_mismatch_stops_loading_and_retains_cleanup(self):
        settings = replace(self.hf.contribution.configuration, checkpoint_sha256="0" * 64)
        contribution = Hf(configuration=settings, checkpoint_path=self.hf.contribution.checkpoint_path, create_model=Model)
        hosted = self.host.adopt(
            contribution=contribution,
            requirements=frozenset({ResourceRequirement(kind="cpu", amount=1, unit="thread", selector="generic")}),
        )
        prediction, _ = self.prediction(hosted=hosted, proposals=(candidate(carbon=False),), name="bad-checkpoint")
        with self.assertRaisesRegex(expected_exception=ValueError, expected_regex="checkpoint"):
            await self.interpret(prediction=prediction, carbon=False)
        self.assertIsNotNone(obj=contribution.model)
        self.assertIsNone(obj=contribution.model.tokenizer)
        self.assertIsNone(obj=prediction.generation)

    async def test_partial_loading_preserves_original_error_and_closes_same_model(self):
        cause = OSError("Cannot finish loading weights")
        models = []

        def fail(model, *, checkpoint):
            model.tokenizer = Tokenizer()
            models.append(model)
            raise cause

        with patch.object(target=Model, attribute="load", new=fail):
            prediction, _ = self.prediction(hosted=self.hf, proposals=(candidate(carbon=False),), name="partial")
            with self.assertRaises(expected_exception=OSError) as raised:
                await self.interpret(prediction=prediction, carbon=False)
        self.assertIs(expr1=cause, expr2=raised.exception)
        model, = models
        self.assertIs(expr1=model, expr2=self.hf.contribution.model)
        self.assertIsNotNone(obj=model.tokenizer)
        await observe_cleanup(control=prediction.execution.close())
        await self.retire()
        self.assertIsNone(obj=model.tokenizer)

    async def test_retirement_before_first_use_does_not_load_model(self):
        await self.retire()
        self.assertIsNone(obj=self.hf.contribution.model)
        self.assertIsNone(obj=self.chf.contribution.model)


class InstalledCommandTests(unittest.TestCase):
    def test_installed_command_runs_both_offerings(self):
        package = Path(str(resources.files(anchor="nmrpeak_mock_provider")))
        distribution = metadata.distribution(distribution_name="nmrpeak-mock-provider-example")
        self.assertEqual(first=package, second=Path(distribution.locate_file(path="nmrpeak_mock_provider")))
        for beam_size in (1, 2):
            with self.subTest(beam_size=beam_size), TemporaryDirectory() as directory:
                shutil.copytree(src=Path(__file__).parent / "config", dst=Path(directory) / "config")
                configuration = Path(directory) / "config/deployments/localhost/provider.toml"
                configuration.write_text(
                    data=configuration.read_text(encoding="utf-8").replace("beam_size = 2", f"{beam_size=}"),
                    encoding="utf-8",
                )
                result = subprocess.run(
                    args=[sys.executable, "-I", "-B", "-m", "nmrpeak_mock_provider"],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(first=0, second=result.returncode, msg=result.stderr)
                reports = [json.loads(s=line) for line in result.stdout.splitlines()]
                self.assertEqual(first=2, second=len(reports))
                self.assertNotEqual(first=reports[0]["checkpoint_sha256"], second=reports[1]["checkpoint_sha256"])
                for report in reports:
                    self.assertEqual(first=beam_size, second=len(report["generated_smiles"]))
