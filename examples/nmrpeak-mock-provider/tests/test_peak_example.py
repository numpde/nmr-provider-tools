"""Exercise the installed provider through its real setup and request entrypoints.

Fixtures supply external configuration, recorded model replies and receivers.
Provider code owns scientific assembly and SDK lifetimes. Direct SDK calls appear
only when injecting a boundary failure or checking the provider's state-port law.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from copy import deepcopy
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

# tomli_w writes TOML; the standard library's tomllib only reads it.
# Tests edit temporary deployment files to exercise real provider startup.
import tomli_w

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
        self.configuration = directory / "config/deployments/localhost/provider.toml"
        for name in ("config", "models"):
            shutil.copytree(
                src=Path(__file__).parent / name,
                dst=directory / name,
            )

        self.enterContext(patch.object(
            target=provider_setup,
            attribute="PROVIDER_CONFIG_PATH",
            new=self.configuration,
        ))

    def configure_science(self, *, variant: str, **changes) -> None:
        """Change raw external deployment input; real startup still owns admission."""
        document = tomllib.loads(self.configuration.read_text(encoding="utf-8"))
        configured, = document["science"][variant]
        configured["settings"].update(changes)
        self.configuration.write_text(data=tomli_w.dumps(document), encoding="utf-8")

    async def test_external_deployment_settings_change_scientific_results(self):
        package = Path(str(resources.files(anchor="nmrpeak_mock_provider")))
        distribution = metadata.distribution(distribution_name="nmrpeak-mock-provider-example")
        self.assertEqual(first=package, second=Path(distribution.locate_file(path="nmrpeak_mock_provider")))
        for beam_size in (1, 2):
            with self.subTest(beam_size=beam_size):
                self.configure_science(variant="hf", beam_size=beam_size)
                self.configure_science(variant="chf", beam_size=beam_size)
                prediction_reports = []
                async with self.open_provider() as provider:
                    for hosted in provider.offerings:
                        self.factory.proposals = (candidate(carbon=isinstance(hosted.contribution, Chf)),)
                        receiver = RecordingReceiver()
                        async with provider.open_prediction(offering=hosted, receive=receiver) as prediction_workflow:
                            await prediction_workflow.interpret(source="Recorded NMR source")
                            await prediction_workflow.deliver()
                            payload, = receiver.payloads
                            prediction_report = json.loads(s=payload)

                            self.assertEqual(
                                first=hosted.contribution.configuration.checkpoint_sha256,
                                second=prediction_report["checkpoint_sha256"],
                            )

                            observed = await prediction_workflow.generation_submission.operation.observe(after=None, wait_seconds=0)
                            self.assertIs(expr1=ResultState.RELEASED, expr2=observed.result_state)
                            prediction_reports.append(prediction_report)
                self.assertEqual(first=2, second=len(prediction_reports))
                self.assertNotEqual(first=prediction_reports[0]["checkpoint_sha256"], second=prediction_reports[1]["checkpoint_sha256"])
                for prediction_report in prediction_reports:
                    self.assertEqual(first=beam_size, second=len(prediction_report["generated_smiles"]))

    async def test_configured_inventory_supports_single_families_and_multiple_models(self):
        original = self.configuration.read_text(encoding="utf-8")
        for families in (("hf",), ("chf",), ("hf", "hf"), ("chf", "chf"), ("hf", "chf", "hf")):
            with self.subTest(families=families):
                document = tomllib.loads(original)
                selected = {family: [] for family in families}
                for family in families:
                    # Each model needs independent nested settings before beam_size changes.
                    configured = deepcopy(x=document["science"][family][0])
                    entries = selected[family]
                    configured["settings"]["beam_size"] = len(entries) + 1
                    entries.append(configured)
                document["science"] = selected
                self.configuration.write_text(data=tomli_w.dumps(document), encoding="utf-8")
                async with self.open_provider() as provider:
                    self.assertEqual(first=len(families), second=len(provider.offerings))
                    for offering in provider.offerings:
                        self.factory.proposals = (candidate(carbon=isinstance(offering.contribution, Chf)),)
                        receiver = RecordingReceiver()
                        async with provider.open_prediction(offering=offering, receive=receiver) as prediction_workflow:
                            await prediction_workflow.interpret(source="Formula and selected spectra")
                            await prediction_workflow.deliver()
                        payload, = receiver.payloads
                        prediction_report = json.loads(s=payload)

                        self.assertEqual(
                            first=offering.contribution.configuration.beam_size,
                            second=len(prediction_report["generated_smiles"]),
                        )

    async def test_unknown_scientific_family_fails_during_real_startup(self):
        document = tomllib.loads(self.configuration.read_text(encoding="utf-8"))
        document["science"]["unknown"] = document["science"].pop("chf")
        self.configuration.write_text(data=tomli_w.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(expected_exception=ValueError, expected_regex="unsupported family='unknown'"):
            async with self.open_provider():
                self.fail(msg="An unsupported scientific family must not yield a provider")

    async def test_partial_assembly_failure_retires_the_already_adopted_object(self):
        closed = []
        close = Hf.close

        class InvalidChf(Chf):
            def close(self):
                pass

        def record_close(contribution) -> None:
            closed.append(contribution)
            close(self=contribution)

        # The real host rejects the malformed second declaration. Provider startup
        # must still retire the valid first object that it already handed over.
        with patch(target="nmrpeak_mock_provider.provider.Chf", new=InvalidChf):
            with patch.object(target=Hf, attribute="close", new=record_close):
                with self.assertRaises(expected_exception=TypeError):
                    async with self.open_provider():
                        self.fail(msg="Partial assembly must not yield a provider")
        first, = closed
        self.assertIsInstance(obj=first, cls=Hf)

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
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=proton_formula_predictor, receive=RecordingReceiver()) as prediction_workflow:
                with self.assertRaises(expected_exception=ModelWindowExpired) as raised:
                    await prediction_workflow.preparation_tool.dispatch(
                        name=prediction_workflow.preparation_tool.name,
                        arguments=candidate(carbon=False),
                        not_after=asyncio.get_running_loop().time() - 1,
                    )

                self.assertIsInstance(obj=raised.exception.__cause__, cls=StartDeadlineExpired)
                self.assertIsNone(obj=prediction_workflow.preparation_tool.operation)
                self.assertIsNone(obj=proton_formula_predictor.contribution.model)

    async def test_interpreter_deadline_waits_for_original_loading_to_settle(self):
        entered = Event()
        release = Event()

        class HeldModel(Model):
            def load(self, *, checkpoint):
                entered.set()
                release.wait(timeout=5)
                return super().load(checkpoint=checkpoint)

        async with self.open_provider(create_model=HeldModel) as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=proton_formula_predictor, receive=RecordingReceiver()) as prediction_workflow:
                deadline = asyncio.get_running_loop().time() + 1

                task = asyncio.create_task(coro=prediction_workflow.preparation_tool.dispatch(
                    name=prediction_workflow.preparation_tool.name,
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
                    self.assertIsNotNone(obj=prediction_workflow.preparation_tool.operation)
                    observed = await prediction_workflow.preparation_tool.operation.observe(after=None, wait_seconds=0)
                    self.assertTrue(expr=observed.finished)
                    self.assertIsNone(obj=prediction_workflow.preparation_tool.prepared_state_handle)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_hf_input_rejects_carbon_fields_before_scientific_validation(self):
        self.factory.proposals = (candidate(carbon=True), candidate(carbon=False))
        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=proton_formula_predictor, receive=RecordingReceiver()) as prediction_workflow:
                await prediction_workflow.interpret(source="Formula and proton spectrum")
                self.assertIsNotNone(obj=prediction_workflow.preparation_tool.operation)
                self.assertEqual(first=2, second=len(self.factory.endpoints[0].requests))

    async def test_generation_lost_submission_reply_resumes_original_scientific_work(self):
        self.factory.proposals = (candidate(carbon=False),)
        original = OSError("The generation submission reply was lost")
        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            receiver = RecordingReceiver()
            async with provider.open_prediction(offering=proton_formula_predictor, receive=receiver) as prediction_workflow:
                await prediction_workflow.interpret(source="Formula and proton spectrum")
                submit = prediction_workflow.analysis_executor.submit
                requests = []
                operations = []

                def lose_reply(*, invocation):
                    requests.append(invocation)
                    operation = submit(invocation=invocation)
                    operations.append(operation)
                    if len(requests) == 1:
                        raise original
                    return operation

                with patch.object(target=prediction_workflow.analysis_executor, attribute="submit", new=lose_reply):
                    with self.assertRaises(expected_exception=OSError) as raised:
                        await prediction_workflow.finish()
                    self.assertIs(expr1=original, expr2=raised.exception)
                    await prediction_workflow.deliver()
                self.assertIs(expr1=requests[0], expr2=requests[1])
                self.assertIs(expr1=operations[0], expr2=operations[1])
                self.assertEqual(first=1, second=len(receiver.payloads))

    async def test_generation_declares_consuming_input_and_does_not_run_twice(self):
        self.factory.proposals = (candidate(carbon=False),)
        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=proton_formula_predictor, receive=RecordingReceiver()) as prediction_workflow:
                await prediction_workflow.interpret(source="Formula and proton spectrum")
                result_ref = await prediction_workflow.finish()
                self.assertEqual(first=result_ref, second=await prediction_workflow.finish())
                reservation, = prediction_workflow.analysis_executor.reservations

                # Probe the provider's actual port declaration: a second scientific
                # submission must be refused before trying to reuse consumed input.
                duplicate = prediction_workflow.analysis_executor.submit_method(
                    method=proton_formula_predictor.contribution.generate,
                    states=(PeakContribution.candidate.bind(handle=prediction_workflow.preparation_tool.prepared_state_handle),),
                    reservation=reservation,
                    policy=prediction_workflow.operation_policy,
                )

                self.assertIsInstance(obj=duplicate, cls=NotAdmitted)
                self.assertIs(expr1=AdmissionCode.INVALID_REFERENCE, expr2=duplicate.code)

    async def test_invalid_generated_candidates_fail_without_reinterpretation(self):
        self.factory.proposals = (candidate(carbon=False),)
        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            settings = proton_formula_predictor.contribution.configuration
            invalid_outputs = ((), ("",), ("C",) * (settings.beam_size + 1), ("C" * (settings.maximum_candidate_characters + 1),))
            for output in invalid_outputs:
                with self.subTest(output=output):
                    async with provider.open_prediction(offering=proton_formula_predictor, receive=RecordingReceiver()) as prediction_workflow:
                        await prediction_workflow.interpret(source="Formula and proton spectrum")
                        with patch.object(target=Model, attribute="generate", return_value=output) as generate:
                            with self.assertRaises(expected_exception=ValueError) as raised:
                                await prediction_workflow.finish()
                            with self.assertRaises(expected_exception=ValueError) as repeated:
                                await prediction_workflow.finish()
                        self.assertIs(expr1=raised.exception, expr2=repeated.exception)
                        self.assertEqual(first=1, second=generate.call_count)
                        self.assertEqual(first=1, second=len(self.factory.endpoints[-1].requests))

    async def test_checkpoint_mismatch_stops_loading_before_model_deserialization(self):
        self.configure_science(variant="hf", checkpoint_sha256="0" * 64)
        self.factory.proposals = (candidate(carbon=False),)
        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            with self.assertRaisesRegex(expected_exception=ValueError, expected_regex="checkpoint"):
                async with provider.open_prediction(offering=proton_formula_predictor, receive=RecordingReceiver()) as prediction_workflow:
                    await prediction_workflow.interpret(source="Formula and proton spectrum")
            self.assertIsNotNone(obj=proton_formula_predictor.contribution.model)
            self.assertIsNone(obj=proton_formula_predictor.contribution.model.tokenizer)
            self.assertIsNone(obj=prediction_workflow.generation_submission)
            self.assertTrue(expr=prediction_workflow.analysis_executor.closed)
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
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            with self.assertRaises(expected_exception=OSError) as raised:
                async with provider.open_prediction(offering=proton_formula_predictor, receive=receiver) as prediction_workflow:
                    await prediction_workflow.interpret(source="Formula and proton spectrum")
            model, = models
            self.assertIs(expr1=original, expr2=raised.exception)
            self.assertEqual(first=[], second=receiver.payloads)
            self.assertIs(expr1=model, expr2=proton_formula_predictor.contribution.model)
            self.assertIsNotNone(obj=model.tokenizer)
            self.assertTrue(expr=prediction_workflow.analysis_executor.closed)
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
        retirement = await provider.execution_host.retire().observe(after=None, wait_seconds=0)
        self.assertTrue(expr=retirement.settled)
        self.assertEqual(first=[], second=created)

    async def test_request_corrects_input_and_concurrent_delivery_sends_once(self):
        rejected = candidate(carbon=False)
        rejected["proton_peaks"] = []
        self.factory.proposals = (rejected, candidate(carbon=False))
        receiver = RecordingReceiver()
        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=proton_formula_predictor, receive=receiver) as prediction_workflow:
                await prediction_workflow.interpret(source="Formula and proton spectrum")
                await asyncio.gather(prediction_workflow.deliver(), prediction_workflow.deliver())
                result_consumer = prediction_workflow.result_consumer
                await prediction_workflow.deliver()
                self.assertIs(expr1=result_consumer, expr2=prediction_workflow.result_consumer)
                self.assertEqual(first=2, second=len(self.factory.endpoints[0].requests))
                self.assertEqual(first=1, second=len(receiver.payloads))
            self.assertTrue(expr=prediction_workflow.analysis_executor.closed)
            self.assertTrue(expr=self.factory.endpoints[0].retirement_confirmed)

    async def test_failed_receiver_keeps_original_result_until_request_exit(self):
        self.factory.proposals = (candidate(carbon=False),)
        original = OSError("Receiver acknowledgement was lost")
        attempts = []

        async def receive(payload):
            attempts.append(payload)
            raise original

        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            with self.assertRaises(expected_exception=OSError) as exit_failure:
                async with provider.open_prediction(offering=proton_formula_predictor, receive=receive) as prediction_workflow:
                    await prediction_workflow.interpret(source="Formula and proton spectrum")
                    for _ in range(2):
                        with self.assertRaises(expected_exception=OSError) as delivery_failure:
                            await prediction_workflow.deliver()
                        self.assertIs(expr1=original, expr2=delivery_failure.exception)
                    retained = await prediction_workflow.generation_submission.operation.read_result(prediction_workflow.result_consumer.result)
                    self.assertEqual(first=attempts[0], second=retained)
                    self.assertFalse(expr=prediction_workflow.analysis_executor.closed)
            self.assertIs(expr1=original, expr2=exit_failure.exception)
            self.assertEqual(first=1, second=len(attempts))
            self.assertTrue(expr=prediction_workflow.analysis_executor.closed)
            self.assertTrue(expr=self.factory.endpoints[0].retirement_confirmed)

    async def test_cancelled_request_joins_receiver_before_releasing_science(self):
        self.factory.proposals = (candidate(carbon=False),)
        entered = asyncio.Event()
        body_left = asyncio.Event()
        allow_receiver_acceptance = asyncio.Event()
        attempts = []
        requests = []

        async def receive(payload):
            attempts.append(payload)
            entered.set()
            await allow_receiver_acceptance.wait()
            self.assertFalse(expr=requests[0].analysis_executor.closed)

        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]

            async def run_request():
                async with provider.open_prediction(offering=proton_formula_predictor, receive=receive) as prediction_workflow:
                    requests.append(prediction_workflow)
                    await prediction_workflow.interpret(source="Formula and proton spectrum")
                    try:
                        await prediction_workflow.deliver()
                    finally:
                        body_left.set()

            task = asyncio.create_task(coro=run_request())
            try:
                async with asyncio.timeout(10):
                    await entered.wait()
                    task.cancel()
                    await body_left.wait()
                    self.assertFalse(expr=task.done())
                    allow_receiver_acceptance.set()
                    with self.assertRaises(expected_exception=asyncio.CancelledError):
                        await task
            finally:
                allow_receiver_acceptance.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(first=1, second=len(attempts))
            self.assertTrue(expr=requests[0].analysis_executor.closed)
            self.assertTrue(expr=self.factory.endpoints[0].retirement_confirmed)

    async def test_foreign_offering_is_rejected_before_interpretation(self):
        async with self.open_provider() as provider:
            async with self.open_provider() as other:
                foreign = next(iter(other.offerings))
                with self.assertRaises(expected_exception=ValueError):
                    async with provider.open_prediction(offering=foreign, receive=RecordingReceiver()):
                        self.fail(msg="A foreign offering must not open a prediction_workflow")
            self.assertEqual(first=[], second=self.factory.endpoints)

    async def test_provider_reuses_model_with_fresh_interpretation_per_request(self):
        self.factory.proposals = (candidate(carbon=False),)
        models = []

        def create_model():
            model = Model()
            models.append(model)
            return model

        async with self.open_provider(create_model=create_model) as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            for _ in range(2):
                receiver = RecordingReceiver()
                async with provider.open_prediction(offering=proton_formula_predictor, receive=receiver) as prediction_workflow:
                    await prediction_workflow.interpret(source="Formula and proton spectrum")
                    await prediction_workflow.deliver()
                self.assertEqual(first=1, second=len(receiver.payloads))
            self.assertEqual(first=1, second=len(models))
            self.assertIs(expr1=models[0], expr2=proton_formula_predictor.contribution.model)
            self.assertEqual(first=2, second=len(self.factory.endpoints))
            self.assertTrue(expr=all(endpoint.retirement_confirmed for endpoint in self.factory.endpoints))
        self.assertIsNone(obj=models[0].tokenizer)

    async def test_occupied_offering_refuses_second_request_without_disrupting_first(self):
        self.factory.proposals = (candidate(carbon=False),)
        receiver = RecordingReceiver()
        async with self.open_provider() as provider:
            proton_formula_predictor, = [offering for offering in provider.offerings if isinstance(offering.contribution, Hf)]
            async with provider.open_prediction(offering=proton_formula_predictor, receive=receiver) as original:
                with self.assertRaises(expected_exception=CapacityUnavailable):
                    async with provider.open_prediction(offering=proton_formula_predictor, receive=RecordingReceiver()):
                        self.fail(msg="One scientific allocation cannot serve two active requests")
                await original.interpret(source="Formula and proton spectrum")
                await original.deliver()
                self.assertEqual(first=1, second=len(receiver.payloads))
