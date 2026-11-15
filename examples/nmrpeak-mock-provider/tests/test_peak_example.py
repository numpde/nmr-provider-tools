"""Exercise the installed provider through its real setup and request entrypoints.

Fixtures supply external configuration, recorded model replies and receivers.
Provider code owns scientific assembly and SDK lifetimes. Direct SDK calls appear
only when injecting a boundary failure or checking the provider's state-port law.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from functools import partial
from importlib import metadata, resources
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Event
import tomllib
import unittest
from unittest.mock import Mock, patch

import tomli_w

from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import AdmissionCode, CapacityUnavailable, NotAdmitted, ResultState, StartDeadlineExpired
from provider_core.interpreter import ModelWindowExpired, ProseService

from nmrpeak_mock_provider import provider as provider_setup
from nmrpeak_mock_provider.binding import Chf, Hf, PeakContribution
from nmrpeak_mock_provider.fake_model import Model, Tokenizer

from .fakes import RecordingReceiver, ScriptedEndpointFactory


def candidate(*, carbon):
    document = {"formula": "C2H6O", "proton_peaks": [{
        "centroid": "1.2", "integral": 3, "multiplicity": "triplet", "couplings": ["7"],
    }]}
    if carbon:
        document["carbon_peaks"] = ["18", "58"]
    return document


class PeakExampleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.factory = ScriptedEndpointFactory(proposals=(), context_bytes=32768)
        # Bind only test substitutes to the actual startup function. Startup itself
        # reads configuration and constructs every scientific object and SDK owner.
        self.open_provider = partial(
            provider_setup.open_provider,
            create_model=Model,
            endpoint_factories=(ProseService(factory=self.factory),),
            secrets=(),
        )
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.configuration = directory / "provider.toml"
        shutil.copyfile(
            src=Path(__file__).parent / "config/deployments/localhost/provider.toml",
            dst=self.configuration,
        )
        self.enterContext(patch.object(
            target=provider_setup,
            attribute="PROVIDER_CONFIG_PATH",
            new=self.configuration,
        ))

    def configure_science(self, *, variant: str, **changes) -> None:
        """Change raw external deployment input; real startup still owns admission."""
        document = tomllib.loads(self.configuration.read_text(encoding="utf-8"))
        document["science"][variant]["settings"].update(changes)
        self.configuration.write_text(data=tomli_w.dumps(document), encoding="utf-8")

    async def test_external_deployment_settings_change_scientific_results(self):
        package = Path(str(resources.files(anchor="nmrpeak_mock_provider")))
        distribution = metadata.distribution(distribution_name="nmrpeak-mock-provider-example")
        self.assertEqual(first=package, second=Path(distribution.locate_file(path="nmrpeak_mock_provider")))
        for beam_size in (1, 2):
            with self.subTest(beam_size=beam_size):
                self.configure_science(variant="hf", beam_size=beam_size)
                self.configure_science(variant="chf", beam_size=beam_size)
                reports = []
                async with self.open_provider() as provider:
                    for hosted in provider.offerings:
                        self.factory.proposals = (candidate(carbon=isinstance(hosted.contribution, Chf)),)
                        receiver = RecordingReceiver()
                        async with provider.open_prediction(offering=hosted, receive=receiver) as prediction:
                            await prediction.interpret(source="Recorded NMR source")
                            await prediction.deliver()
                            payload, = receiver.payloads
                            report = json.loads(s=payload)
                            self.assertEqual(
                                first=hosted.contribution.configuration.checkpoint_sha256,
                                second=report["checkpoint_sha256"],
                            )
                            observed = await prediction.generation.observe(after=None, wait_seconds=0)
                            self.assertIs(expr1=ResultState.RELEASED, expr2=observed.result_state)
                            reports.append(report)
                self.assertEqual(first=2, second=len(reports))
                self.assertNotEqual(first=reports[0]["checkpoint_sha256"], second=reports[1]["checkpoint_sha256"])
                for report in reports:
                    self.assertEqual(first=beam_size, second=len(report["generated_smiles"]))

    async def test_partial_assembly_failure_retires_the_already_adopted_object(self):
        original = OSError("Cannot adopt the second scientific offering")
        adopted = []
        closed = []
        adopt = EmbeddedHost.adopt
        close = Hf.close

        def fail_second(owner, *, contribution, requirements):
            if adopted:
                raise original
            hosted = adopt(self=owner, contribution=contribution, requirements=requirements)
            adopted.append(hosted)
            return hosted

        def record_close(contribution) -> None:
            closed.append(contribution)
            close(self=contribution)

        # Fault the real acquisition boundary; keep the provider's actual assembly.
        with patch.object(target=EmbeddedHost, attribute="adopt", new=fail_second):
            with patch.object(target=Hf, attribute="close", new=record_close):
                with self.assertRaises(expected_exception=OSError) as raised:
                    async with self.open_provider():
                        self.fail(msg="Partial assembly must not yield a provider")
        self.assertIs(expr1=original, expr2=raised.exception)
        self.assertEqual(first=[adopted[0].contribution], second=closed)

    async def test_invalid_deployment_model_limits_fail_before_model_creation(self):
        original = self.configuration.read_text(encoding="utf-8")
        create_model = Mock(side_effect=AssertionError("Invalid settings must not construct a model"))
        for field in ("beam_size", "maximum_input_tokens", "maximum_candidate_characters"):
            with self.subTest(field=field):
                self.configuration.write_text(data=original, encoding="utf-8")
                self.configure_science(variant="hf", **{field: 0})
                with self.assertRaises(expected_exception=ValueError):
                    async with self.open_provider(create_model=create_model):
                        self.fail(msg="Invalid scientific settings must fail during startup")
        create_model.assert_not_called()

    async def test_expired_interpretation_window_admits_no_scientific_work(self):
        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=hf, receive=RecordingReceiver()) as prediction:
                with self.assertRaises(expected_exception=ModelWindowExpired) as raised:
                    await prediction.dispatch(
                        name=prediction.input_tool.name,
                        arguments=candidate(carbon=False),
                        not_after=asyncio.get_running_loop().time() - 1,
                    )
                self.assertIsInstance(obj=raised.exception.__cause__, cls=StartDeadlineExpired)
                self.assertEqual(first=[], second=prediction.preparations)
                self.assertIsNone(obj=hf.contribution.model)

    async def test_interpreter_deadline_waits_for_original_loading_to_settle(self):
        entered = Event()
        release = Event()

        class HeldModel(Model):
            def load(self, *, checkpoint):
                entered.set()
                release.wait(timeout=5)
                return super().load(checkpoint=checkpoint)

        async with self.open_provider(create_model=HeldModel) as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=hf, receive=RecordingReceiver()) as prediction:
                deadline = asyncio.get_running_loop().time() + 1
                task = asyncio.create_task(coro=prediction.dispatch(
                    name=prediction.input_tool.name,
                    arguments=candidate(carbon=False),
                    not_after=deadline,
                ))
                try:
                    async with asyncio.timeout(10):
                        self.assertTrue(expr=await asyncio.to_thread(entered.wait, timeout=2))
                        await asyncio.sleep(delay=max(0, deadline - asyncio.get_running_loop().time()) + 0.01)
                        self.assertFalse(expr=task.done())
                        release.set()
                        with self.assertRaises(expected_exception=ModelWindowExpired) as raised:
                            await task
                    self.assertIsInstance(obj=raised.exception.__cause__, cls=StartDeadlineExpired)
                    self.assertEqual(first=1, second=len(prediction.preparations))
                    observed = await prediction.preparations[0].observe(after=None, wait_seconds=0)
                    self.assertTrue(expr=observed.finished)
                    self.assertIsNone(obj=prediction.prepared)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_hf_input_rejects_carbon_fields_before_scientific_validation(self):
        self.factory.proposals = (candidate(carbon=True), candidate(carbon=False))
        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=hf, receive=RecordingReceiver()) as prediction:
                await prediction.interpret(source="Formula and proton spectrum")
                self.assertEqual(first=1, second=len(prediction.preparations))
                self.assertEqual(first=2, second=len(self.factory.endpoints[0].requests))

    async def test_generation_declares_consuming_input_and_does_not_run_twice(self):
        self.factory.proposals = (candidate(carbon=False),)
        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=hf, receive=RecordingReceiver()) as prediction:
                await prediction.interpret(source="Formula and proton spectrum")
                result = await prediction.finish()
                self.assertEqual(first=result, second=await prediction.finish())
                reservation, = prediction.execution.reservations
                # Probe the provider's actual port declaration: a second scientific
                # submission must be refused before trying to reuse consumed input.
                duplicate = prediction.execution.submit_method(
                    method=hf.contribution.generate,
                    states=(PeakContribution.candidate.bind(handle=prediction.prepared),),
                    reservation=reservation,
                    policy=prediction.policy,
                )
                self.assertIsInstance(obj=duplicate, cls=NotAdmitted)
                self.assertIs(expr1=AdmissionCode.INVALID_REFERENCE, expr2=duplicate.code)

    async def test_invalid_generated_candidates_fail_without_reinterpretation(self):
        self.factory.proposals = (candidate(carbon=False),)
        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            settings = hf.contribution.configuration
            invalid_outputs = ((), ("",), ("C",) * (settings.beam_size + 1), ("C" * (settings.maximum_candidate_characters + 1),))
            for output in invalid_outputs:
                with self.subTest(output=output):
                    async with provider.open_prediction(offering=hf, receive=RecordingReceiver()) as prediction:
                        await prediction.interpret(source="Formula and proton spectrum")
                        with patch.object(target=Model, attribute="generate", return_value=output) as generate:
                            with self.assertRaises(expected_exception=ValueError) as raised:
                                await prediction.finish()
                            with self.assertRaises(expected_exception=ValueError) as repeated:
                                await prediction.finish()
                        self.assertIs(expr1=raised.exception, expr2=repeated.exception)
                        self.assertEqual(first=1, second=generate.call_count)
                        self.assertEqual(first=1, second=len(self.factory.endpoints[-1].requests))

    async def test_checkpoint_mismatch_stops_loading_before_model_deserialization(self):
        self.configure_science(variant="hf", checkpoint_sha256="0" * 64)
        self.factory.proposals = (candidate(carbon=False),)
        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            with self.assertRaisesRegex(expected_exception=ValueError, expected_regex="checkpoint"):
                async with provider.open_prediction(offering=hf, receive=RecordingReceiver()) as prediction:
                    await prediction.interpret(source="Formula and proton spectrum")
            self.assertIsNotNone(obj=hf.contribution.model)
            self.assertIsNone(obj=hf.contribution.model.tokenizer)
            self.assertIsNone(obj=prediction.generation)
            self.assertTrue(expr=prediction.execution.closed)
            self.assertTrue(expr=self.factory.endpoints[0].retirement_confirmed)

    async def test_partial_loading_preserves_original_error_and_closes_same_model(self):
        original = OSError("Cannot finish loading weights")
        models = []
        closed = []
        receiver = RecordingReceiver()
        self.factory.proposals = (candidate(carbon=False),)

        class PartialModel(Model):
            def load(self, *, checkpoint):
                self.tokenizer = Tokenizer()
                models.append(self)
                raise original

            def close(self) -> None:
                closed.append(self)
                super().close()

        async with self.open_provider(create_model=PartialModel) as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            with self.assertRaises(expected_exception=OSError) as raised:
                async with provider.open_prediction(offering=hf, receive=receiver) as prediction:
                    await prediction.interpret(source="Formula and proton spectrum")
            model, = models
            self.assertIs(expr1=original, expr2=raised.exception)
            self.assertEqual(first=[], second=receiver.payloads)
            self.assertIs(expr1=model, expr2=hf.contribution.model)
            self.assertIsNotNone(obj=model.tokenizer)
            self.assertTrue(expr=prediction.execution.closed)
            self.assertTrue(expr=self.factory.endpoints[0].retirement_confirmed)
        self.assertEqual(first=[model], second=closed)
        self.assertIsNone(obj=model.tokenizer)

    async def test_setup_does_not_start_science_and_retires_on_exit(self):
        created = []

        def create_model():
            model = Model()
            created.append(model)
            return model

        async with self.open_provider(create_model=create_model) as provider:
            self.assertEqual(first=2, second=len(provider.offerings))
            self.assertEqual(first=[], second=created)
        retirement = await provider.host.retire().observe(after=None, wait_seconds=0)
        self.assertTrue(expr=retirement.settled)
        self.assertEqual(first=[], second=created)

    async def test_request_corrects_input_and_concurrent_delivery_sends_once(self):
        rejected = candidate(carbon=False)
        rejected["proton_peaks"] = []
        self.factory.proposals = (rejected, candidate(carbon=False))
        receiver = RecordingReceiver()
        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=hf, receive=receiver) as prediction:
                await prediction.interpret(source="Formula and proton spectrum")
                await asyncio.gather(prediction.deliver(), prediction.deliver())
                consumption = prediction.consumption
                await prediction.deliver()
                self.assertIs(expr1=consumption, expr2=prediction.consumption)
                self.assertEqual(first=2, second=len(prediction.preparations))
                self.assertEqual(first=1, second=len(receiver.payloads))
            self.assertTrue(expr=prediction.execution.closed)
            self.assertTrue(expr=self.factory.endpoints[0].retirement_confirmed)

    async def test_failed_receiver_keeps_original_result_until_request_exit(self):
        self.factory.proposals = (candidate(carbon=False),)
        original = OSError("Receiver acknowledgement was lost")
        attempts = []

        async def receive(payload):
            attempts.append(payload)
            raise original

        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            with self.assertRaises(expected_exception=OSError) as exit_failure:
                async with provider.open_prediction(offering=hf, receive=receive) as prediction:
                    await prediction.interpret(source="Formula and proton spectrum")
                    for _ in range(2):
                        with self.assertRaises(expected_exception=OSError) as delivery_failure:
                            await prediction.deliver()
                        self.assertIs(expr1=original, expr2=delivery_failure.exception)
                    retained = await prediction.generation.read_result(prediction.consumption.result)
                    self.assertEqual(first=attempts[0], second=retained)
                    self.assertFalse(expr=prediction.execution.closed)
            self.assertIs(expr1=original, expr2=exit_failure.exception)
            self.assertEqual(first=1, second=len(attempts))
            self.assertTrue(expr=prediction.execution.closed)
            self.assertTrue(expr=self.factory.endpoints[0].retirement_confirmed)

    async def test_cancelled_request_joins_receiver_before_releasing_science(self):
        self.factory.proposals = (candidate(carbon=False),)
        entered = asyncio.Event()
        body_left = asyncio.Event()
        accepted = asyncio.Event()
        attempts = []
        requests = []

        async def receive(payload):
            attempts.append(payload)
            entered.set()
            await accepted.wait()
            self.assertFalse(expr=requests[0].execution.closed)

        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]

            async def run_request():
                async with provider.open_prediction(offering=hf, receive=receive) as prediction:
                    requests.append(prediction)
                    await prediction.interpret(source="Formula and proton spectrum")
                    try:
                        await prediction.deliver()
                    finally:
                        body_left.set()

            task = asyncio.create_task(coro=run_request())
            try:
                async with asyncio.timeout(10):
                    await entered.wait()
                    task.cancel()
                    await body_left.wait()
                    self.assertFalse(expr=task.done())
                    accepted.set()
                    with self.assertRaises(expected_exception=asyncio.CancelledError):
                        await task
            finally:
                accepted.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(first=1, second=len(attempts))
            self.assertTrue(expr=requests[0].execution.closed)
            self.assertTrue(expr=self.factory.endpoints[0].retirement_confirmed)

    async def test_foreign_offering_is_rejected_before_interpretation(self):
        async with self.open_provider() as provider:
            async with self.open_provider() as other:
                foreign = next(iter(other.offerings))
                with self.assertRaises(expected_exception=ValueError):
                    async with provider.open_prediction(offering=foreign, receive=RecordingReceiver()):
                        self.fail(msg="A foreign offering must not open a prediction")
            self.assertEqual(first=[], second=self.factory.endpoints)

    async def test_provider_reuses_model_with_fresh_interpretation_per_request(self):
        self.factory.proposals = (candidate(carbon=False),)
        models = []

        def create_model():
            model = Model()
            models.append(model)
            return model

        async with self.open_provider(create_model=create_model) as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            for _ in range(2):
                receiver = RecordingReceiver()
                async with provider.open_prediction(offering=hf, receive=receiver) as prediction:
                    await prediction.interpret(source="Formula and proton spectrum")
                    await prediction.deliver()
                self.assertEqual(first=1, second=len(receiver.payloads))
            self.assertEqual(first=1, second=len(models))
            self.assertIs(expr1=models[0], expr2=hf.contribution.model)
            self.assertEqual(first=2, second=len(self.factory.endpoints))
            self.assertTrue(expr=all(endpoint.retirement_confirmed for endpoint in self.factory.endpoints))
        self.assertIsNone(obj=models[0].tokenizer)

    async def test_occupied_offering_refuses_second_request_without_disrupting_first(self):
        self.factory.proposals = (candidate(carbon=False),)
        receiver = RecordingReceiver()
        async with self.open_provider() as provider:
            hf, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=hf, receive=receiver) as original:
                with self.assertRaises(expected_exception=CapacityUnavailable):
                    async with provider.open_prediction(offering=hf, receive=RecordingReceiver()):
                        self.fail(msg="One scientific allocation cannot serve two active requests")
                await original.interpret(source="Formula and proton spectrum")
                await original.deliver()
                self.assertEqual(first=1, second=len(receiver.payloads))
