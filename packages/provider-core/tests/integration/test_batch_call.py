"""API Attempts share a real native batch while retaining separate publication authority."""

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from provider_core.analysis import AnalysisOutcomeUnresolved, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence
from provider_core.batch_call import BatchCall, BatchPolicy
from provider_core.job_service import HostFailurePolicy, JobOffering, JobService, JobServicePolicy
from provider_core.ownership import LiveAttemptLimits
from provider_core.payload import JsonDataclassCodec
from provider_core.execution_setup import ExecutionSettings
from provider_core.scientific import Contribution, operation
from tests.fakes.checkpoint_execution import CausalDelivery
from tests.fakes.job_service import JobApi
from tests.fakes.runner import RunningSupervisor
from tests.fakes.scientific_process import Number
from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering


@dataclass
class Inputs:
    numbers: list[Number]


@dataclass
class Outputs:
    numbers: list[int]


class Science(Contribution):
    def __init__(self, *, startup):
        super().__init__()
        self.witness = Path(startup["witness_directory"])

    @operation()
    def calculate(self, *, batch: Inputs) -> Outputs:
        (self.witness / "entered").touch()

        while not (self.witness / "release").exists():
            time.sleep(0.01)

        calculated = []

        for (index, number) in enumerate(batch.numbers):
            with (self.witness / "calls").open("a") as witness:
                witness.write(str(number.value) + "\n")

            if number.value < 0:
                os._exit(71)

            calculated.append(number.value * 10 + index)

        return Outputs(calculated)


def construct(*, startup):
    return Science(startup=startup)


class Session:
    def __init__(self, batches):
        self.batches = batches
        self.participation = None
        self.retiring = asyncio.Event()
        self.retired = asyncio.Event()

    async def prepare(self, acknowledged, instructions, *, report_condition):
        scientific_input = Number(**json.loads(instructions.specification_bytes))
        self.participation = await self.batches.prepare(scientific_input)

        return PreparedAnalysis(ProtectedEvidence("test.input.v1", instructions.specification_bytes))

    async def execute(self, prepared, *, report_condition):
        try:
            scientific_result = await self.participation.execute(require_execution=report_condition.require_execution)
        except Exception as error:
            raise AnalysisOutcomeUnresolved("Native batch returned no complete result") from error

        content = json.dumps(scientific_result).encode()

        return AnalysisSuccess("test.result.v1", content, ProtectedEvidence("test.result.v1", content))

    async def retire(self):
        self.retiring.set()

        if self.participation is not None:
            await self.participation.retire()

        self.retired.set()


class NativeBatchTests(unittest.IsolatedAsyncioTestCase):
    async def setup_batch(self):
        directory = self.enterContext(TemporaryDirectory())
        witness = Path(directory)

        runner = RunningSupervisor(
            operation_seconds=None,
            role="science",
            worker_argv=(sys.executable, "-I", "-B", str(Path(__file__).parent.parent / "fakes/scientific_process_worker.py")),
            environment={"PROVIDER_CORE_IN_CONTAINER": os.environ["PROVIDER_CORE_IN_CONTAINER"]},
        )

        self.addCleanup(runner.close)

        settings = ExecutionSettings(
            {
                "backend": "process",
                "host": {
                    "input_bytes": 4096,
                    "result_bytes": 4096,
                    "state_values": 1,
                    "progress_records": 1,
                    "initialization_seconds": 5,
                    "retirement_seconds": 5,
                },
                "operation_policy": {
                    "queue_stop_request_seconds": 5,
                    "execution_stop_request_seconds": 10,
                    "cleanup_overdue_flag_seconds": 5,
                    "reject_encoded_input_over_bytes": 4096,
                    "reject_encoded_result_over_bytes": 4096,
                },
                "resource_requirement": [],
            },
        )

        call = settings.open_process_call(
            runner={
                "socket": str(runner.socket_path),
                "lease_file": str(runner.endpoint / ".owner-session.lock"),
                "role": "science",
                "startup_seconds": 5,
                "retirement_seconds": 5,
            },
            contribution_type=Science,
            constructor=construct,
            startup={"witness_directory": str(witness), "scenario": "healthy"},
            method=Science.calculate,
        )

        await call.start()

        batches = BatchCall(
            call=call,
            policy=BatchPolicy(maximum_entries=3, collection_seconds=0.1, maximum_queued_input_bytes=4096, maximum_entry_input_bytes=1024),
            entry_codec=JsonDataclassCodec(format="test.number.v1", value_type=Number),
            pack=lambda numbers: Inputs(list(numbers)),
            unpack=lambda batch_reply: batch_reply.numbers,
        )

        return (witness, call, batches)

    async def wait_file(self, path):
        async with asyncio.timeout(5):
            while not path.exists():
                await asyncio.sleep(0.01)

    async def test_revoked_job_waits_for_native_batch_while_siblings_deliver_independently(self):
        (witness, call, batches) = await self.setup_batch()
        fixture = JobApi()
        fixture.jobs = {f"job:{index}": ("numbers", b'{"value":2}') for index in range(3)}
        stopping = asyncio.Event()
        sessions = []
        deliveries = []

        def new_session():
            session = Session(batches)

            sessions.append(session)

            return session

        def new_delivery():
            delivery = CausalDelivery()

            deliveries.append(delivery)

            return delivery

        service = JobService(
            provider_ref="provider:example",
            offerings=(
                JobOffering(AnalysisOffering("numbers", "Fixture"), call.host, "test", "one", new_session),
            ),
            host_failure_policy=HostFailurePolicy.STOP,
            limits=LiveAttemptLimits(3, 393216, 393216, 32768),
            maximum_preparations=2,
            display_name="Batch fixture",
            description="Three independent jobs",
            policy=JobServicePolicy(0.01, 10, 0.02, 1, 0.02, 1),
            new_api=fixture.new_api,
            new_delivery=new_delivery,
            stopping=stopping,
        )

        await batches.__aenter__()
        task = asyncio.create_task(service.serve())

        try:
            await self.wait_file(witness / "entered")
            fixture.cancelled_jobs.add("job:0")
            await asyncio.wait_for(sessions[0].retiring.wait(), 3)
            self.assertFalse(sessions[0].retired.is_set())
            self.assertEqual(3, len(service.claims.live_owners()))
            (witness / "release").touch()
            await asyncio.wait_for(deliveries[1].wait_for_offers(1), 3)
            await asyncio.wait_for(deliveries[2].wait_for_offers(1), 3)
            self.assertEqual([], deliveries[0].offers)
            self.assertEqual("job:1", deliveries[1].offers[0].job_ref)
            self.assertEqual("job:2", deliveries[2].offers[0].job_ref)
            deliveries[2].answer("accepted")
            await asyncio.wait_for(deliveries[2].accepted.wait(), 2)
            self.assertFalse(deliveries[1].accepted.is_set())
            self.assertEqual(["2", "2", "2"], (witness / "calls").read_text().splitlines())
            stopping.set()
            deliveries[1].answer("accepted")
            await asyncio.wait_for(task, 5)
            self.assertTrue(service.shutdown_ready)
        finally:
            stopping.set()
            (witness / "release").touch()

            for delivery in deliveries:
                delivery.release_for_teardown()

            await asyncio.wait_for(task, 5)
            await batches.close()
            await call.close()

    async def test_native_exit_after_first_entry_establishes_no_partial_reply(self):
        (witness, call, batches) = await self.setup_batch()

        await batches.__aenter__()
        participants = [await batches.prepare(Number(value=number)) for number in (2, -1, 3)]
        waits = [asyncio.create_task(participant.execute(require_execution=lambda: None)) for participant in participants]

        try:
            await self.wait_file(witness / "entered")
            (witness / "release").touch()
            outcomes = await asyncio.wait_for(asyncio.gather(*waits, return_exceptions=True), 10)

            self.assertTrue(all(isinstance(outcome, Exception) for outcome in outcomes))
            self.assertTrue(all((outcome is outcomes[0]) for outcome in outcomes))
            self.assertEqual(["2", "-1"], (witness / "calls").read_text().splitlines())

            for participant in participants:
                await participant.retire()

            with self.assertRaises(Exception):
                await batches.close()
        finally:
            (witness / "release").touch()

            with self.assertRaises(Exception):
                await call.close()

    async def test_owned_process_excludes_competing_dispatchers_and_direct_calls(self):
        (witness, call, batches) = await self.setup_batch()

        (witness / "release").touch()

        try:
            async with batches:
                competing = BatchCall(
                    call=call,
                    policy=batches.policy,
                    entry_codec=batches.entry_codec,
                    pack=batches.pack,
                    unpack=batches.unpack,
                )

                with self.assertRaises(RuntimeError):
                    await competing.__aenter__()

                with self.assertRaises(RuntimeError):
                    await call(scientific_input=Inputs([Number(value=9)]))

                self.assertFalse((witness / "calls").exists())

                participation = await batches.prepare(Number(value=1))

                self.assertEqual(10, await participation.execute(require_execution=lambda: None))
                await participation.retire()

            self.assertEqual(Outputs([20]), await call(scientific_input=Inputs([Number(value=2)])))
            self.assertEqual(["1", "2"], (witness / "calls").read_text().splitlines())
        finally:
            await call.close()

    async def test_batch_cannot_take_over_an_active_owned_process_call(self):
        (witness, call, batches) = await self.setup_batch()
        direct = asyncio.create_task(call(scientific_input=Inputs([Number(value=1)])))

        try:
            await self.wait_file(witness / "entered")

            with self.assertRaises(RuntimeError):
                await batches.__aenter__()

            self.assertFalse(direct.done())
            (witness / "release").touch()
            self.assertEqual(Outputs([10]), await asyncio.wait_for(direct, 5))

            async with batches:
                self.assertEqual(["1"], (witness / "calls").read_text().splitlines())
        finally:
            (witness / "release").touch()
            await asyncio.gather(direct, return_exceptions=True)
            await call.close()
