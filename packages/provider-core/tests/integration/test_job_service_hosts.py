"""Real native hosts back three API offerings, with one original Attempt held.

The API and delivery replies are scripted ports. Installed SDK hosts, workers,
method bindings, controllers and exit observation execute normally. This proves
local ownership and Hello snapshots, not remote API acceptance or model accuracy.
"""

import asyncio
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import errno
import json
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from provider_core.analysis import AnalysisOutcomeUnresolved, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence
from provider_core.controller import ControllerFailed
from provider_core.hosting import OperationPolicy
from provider_core.job_service import JobOffering, JobService, JobServicePolicy, HostFailurePolicy
from provider_core.ownership import LiveAttemptLimits
from provider_core.process_host import ProcessExecutionHost
from provider_core.process_wire import RemoteScienceError
from provider_core.scientific import Contribution, operation
from provider_core.scientific_call import ErrorPolicy, ScientificCall
from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering
from tests.fakes.api import Transport, api, io_failure, reply
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.hello import receipt
from tests.fakes.job_input import facts, response
from tests.fakes.runner import RunningSupervisor
from tests.fakes.scientific_process import Number


@dataclass(frozen=True)
class Result:
    value: int
    process_id: int


class Science(Contribution):
    def __init__(self, *, startup):
        super().__init__()
        self.witness = Path(startup["witness_directory"])

    def record(self, text):
        with (self.witness / "calls").open("a") as stream:
            stream.write(text + "\n")

    @operation()
    def runtime(self) -> Result:
        self.record("runtime")

        return Result(value=0, process_id=os.getpid())

    @operation()
    def calculate(self, *, number: Number) -> Result:
        self.record(f"calculate:{number.value}")

        if number.value < 0:
            (self.witness / "entered").touch()

            while not (self.witness / "release").exists():
                time.sleep(0.01)

        return Result(value=number.value * 2, process_id=os.getpid())


def construct(*, startup):
    return Science(startup=startup)


class Session:
    def __init__(self, call, *, has_input):
        (self.call, self.has_input) = (call, has_input)

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.input = Number(**json.loads(instructions.specification_bytes))

        return PreparedAnalysis(ProtectedEvidence("test.input.v1", instructions.specification_bytes))

    async def execute(self, prepared, *, report_condition):
        # silent-defaults: allow-contract -- The runtime method has no input; calculate consumes the fixture number.
        arguments = {"scientific_input": self.input} if self.has_input else {}

        try:
            result = await self.call(**arguments, require_execution=report_condition.require_execution)
        except Exception as cause:
            raise AnalysisOutcomeUnresolved("The native fixture returned no verified result") from cause

        content = json.dumps(asdict(result)).encode()

        return AnalysisSuccess("test.result.v1", content, ProtectedEvidence("test.result.v1", content))

    async def retire(self):
        pass


class JobApi:
    def __init__(self):
        self.jobs = {
            "job:runtime": ("runtime", b'{"value":0}'),
            "job:calculate": ("calculate", b'{"value":2}'),
            "job:held": ("calculate", b'{"value":-1}'),
            "job:healthy": ("healthy", b'{"value":3}'),
        }

        self.attempts = {}
        self.started = []
        self.announcements = []
        self.unavailable = False
        self.feed_requests = asyncio.Queue()

    def new_api(self):
        fixture = self

        class Replies(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                url = urlsplit(str(request.url))
                path = unquote(url.path)

                if path.endswith("/hello"):
                    fixture.announcements.append(json.loads(request.content)["analysis_offerings"])

                    return reply(receipt())

                if path.endswith("/jobs"):
                    kind = parse_qs(url.query)["analysis_kind_ref"][0]

                    fixture.feed_requests.put_nowait(kind)

                    if fixture.unavailable:
                        raise io_failure(
                            "The shared API transport is unavailable",
                            primary=ConnectionRefusedError(errno.ECONNREFUSED, "API fixture is offline"),
                            stage="connect",
                        )

                    jobs = [
                        {
                            **asdict(replace(facts(payload), job_ref=job)),
                            "analysis_kind_ref": kind,
                            "created_at": "2026-10-18T00:00:00.123456Z",
                        }
                        for (job, (selected, payload)) in fixture.jobs.items()
                        if (selected == kind) and (job not in fixture.started)
                    ]

                    return reply(
                        json.dumps(
                            {
                                "schema_id": "nmr.provider.jobs.list.response.v1",
                                "analysis_kind_ref": kind,
                                "has_provider_execution_attempt": False,
                                "jobs": jobs,
                                "next_cursor": None,
                            }
                        )
                        .encode()
                    )

                if path.endswith("/input"):
                    job = path.split("/")[-2]

                    return reply(response(fixture.jobs[job][1], job_ref=job))

                if path.endswith("/start"):
                    job = json.loads(request.content)["job_ref"]
                    reference = "execution_attempt:sha256:" + sha256(job.encode()).hexdigest()
                    fixture.attempts[reference] = job

                    fixture.started.append(job)

                    return reply(
                        start_response(
                            execution_attempt_ref=reference,
                            job_ref=job,
                            analysis_kind_ref=fixture.jobs[job][0],
                        )
                    )

                if path.endswith("/progress"):
                    reference = path.split("/")[-2]
                    progress = json.loads(request.content)

                    return reply(
                        progress_response(
                            execution_attempt_ref=reference,
                            phase=progress["phase"],
                            condition_code=progress["condition_code"],
                        )
                    )

                reference = path.split("/")[-1]

                return reply(observation(execution_attempt_ref=reference, job_ref=fixture.attempts[reference]))

        return api(Replies(), "https://api.example")


class JobServiceHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.killed_host = None

    async def host(self, root):
        root.mkdir()
        worker = Path(__file__).parent.parent / "fakes/scientific_process_worker.py"

        runner = RunningSupervisor(
            operation_seconds=None,
            role="science",
            worker_argv=(sys.executable, "-I", "-B", str(worker)),
            environment={"PROVIDER_CORE_IN_CONTAINER": os.environ["PROVIDER_CORE_IN_CONTAINER"]},
        )

        self.addCleanup(runner.close)

        host = ProcessExecutionHost(
            session=runner.session(startup_seconds=5, retirement_seconds=5),
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )

        await host.__aenter__()
        self.addAsyncCleanup(self.close_host, host)

        hosted = await host.start(
            contribution_type=Science,
            constructor=f"{__name__}:construct",
            startup={"witness_directory": str(root), "scenario": "healthy"},
            requirements=frozenset(),
        )

        return (host, hosted)

    async def close_host(self, host):
        if host is self.killed_host:
            with self.assertRaises(ExceptionGroup) as caught:
                await host.__aexit__(None, None, None)

            self.assertTrue(all(isinstance(error, RemoteScienceError) for error in caught.exception.exceptions))
        else:
            await host.__aexit__(None, None, None)

    def offering(self, kind, host, hosted, method):
        call = ScientificCall(
            host=host,
            allocation=hosted.allocation,
            binding=hosted.call(method=method),
            operation_policy=OperationPolicy(5, 10, 5, 1024, 1024),
            error_policy=ErrorPolicy(),
        )

        return JobOffering(
            AnalysisOffering(kind, f"Fixture {kind}"),
            host,
            kind,
            "one",
            lambda: Session(call, has_input=method is Science.calculate),
        )

    async def test_shared_host_loss_withdraws_both_methods_and_preserves_healthy_delivery(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        (shared, shared_science) = await self.host(root / "shared")
        (healthy, healthy_science) = await self.host(root / "healthy")
        (shared_pid, healthy_pid) = (shared.process_id, healthy.process_id)
        jobs = JobApi()
        (stopping, report_entered, release_report) = (asyncio.Event(), asyncio.Event(), asyncio.Event())
        outcomes = []

        class Delivery(RecordingDelivery):
            async def submit(self, item):
                if item.job_ref == "job:healthy":
                    report_entered.set()
                    await release_report.wait()

                outcomes.append(item)

                return await super().submit(item)

        service = JobService(
            provider_ref="provider:example",
            display_name="Fixture",
            description="Native host composition",
            offerings=(
                self.offering("runtime", shared, shared_science, Science.runtime),
                self.offering("calculate", shared, shared_science, Science.calculate),
                self.offering("healthy", healthy, healthy_science, Science.calculate),
            ),
            host_failure_policy=HostFailurePolicy.WITHDRAW,
            limits=LiveAttemptLimits(3, 786432, 393216),
            policy=JobServicePolicy(0.01, 10, 0.05, 1, 0.02, 1),
            new_api=jobs.new_api,
            new_delivery=Delivery,
            stopping=stopping,
        )

        task = asyncio.create_task(service.serve())

        try:
            async with asyncio.timeout(10):
                while not (root / "shared/entered").exists():
                    if task.done():
                        await task

                    await asyncio.sleep(0.01)

            os.kill(shared_pid, signal.SIGKILL)
            self.killed_host = shared

            await asyncio.wait_for(report_entered.wait(), 10)

            async with asyncio.timeout(5):
                while {item["analysis_kind_ref"] for item in jobs.announcements[-1]} != {"healthy"}:
                    await asyncio.sleep(0.01)

            self.assertEqual(["runtime", "calculate:2", "calculate:-1"], (root / "shared/calls").read_text().splitlines())
            self.assertEqual(["calculate:3"], (root / "healthy/calls").read_text().splitlines())
            self.assertEqual(["job:runtime", "job:calculate", "job:held", "job:healthy"], jobs.started)
            self.assertEqual(2, len(service.claims.live_owners()))
            self.assertEqual(["job:runtime", "job:calculate"], [item.job_ref for item in outcomes])
            os.kill(healthy_pid, 0)

            stopping.set()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release_report.set()

            with self.assertRaises(ControllerFailed) as caught:
                await asyncio.wait_for(task, 10)

            (held,) = service.claims.live_owners()

            self.assertIs(held.last_failure, caught.exception)
            self.assertIsInstance(caught.exception.cause.cause, AnalysisOutcomeUnresolved)
            self.assertEqual(["job:runtime", "job:calculate", "job:healthy"], [item.job_ref for item in outcomes])
            self.assertEqual(1, len(service.claims.live_owners()))
            self.assertTrue(service.shutdown_ready)
            self.assertEqual([], jobs.announcements[-1])
        finally:
            (root / "shared/release").touch()
            release_report.set()
            stopping.set()

            if not task.done():
                await asyncio.wait_for(task, 10)

    async def test_shared_api_outage_pauses_all_offerings_without_failing_science(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        (host, science) = await self.host(root / "science")
        jobs = JobApi()
        jobs.jobs = {job: value for (job, value) in jobs.jobs.items() if job in {"job:runtime", "job:calculate"}}
        jobs.unavailable = True
        stopping = asyncio.Event()
        delivered = []

        class Delivery(RecordingDelivery):
            async def submit(self, item):
                delivered.append(item.job_ref)
                result = await super().submit(item)

                if len(delivered) == 2:
                    stopping.set()

                return result

        service = JobService(
            provider_ref="provider:example",
            display_name="Fixture",
            description="Shared API recovery",
            offerings=(
                self.offering("runtime", host, science, Science.runtime),
                self.offering("calculate", host, science, Science.calculate),
            ),
            host_failure_policy=HostFailurePolicy.STOP,
            limits=LiveAttemptLimits(1, 262144, 131072),
            policy=JobServicePolicy(0.01, 10, 0.05, 1, 0.02, 1),
            new_api=jobs.new_api,
            new_delivery=Delivery,
            stopping=stopping,
        )

        task = asyncio.create_task(service.serve())

        try:
            for _ in range(2):
                self.assertEqual("runtime", await asyncio.wait_for(jobs.feed_requests.get(), 5))

            self.assertEqual([], jobs.started)
            self.assertEqual([], delivered)
            self.assertFalse((root / "science/calls").exists())

            self.assertEqual(
                {"runtime", "calculate"},
                {offering["analysis_kind_ref"] for offering in jobs.announcements[-1]},
            )

            jobs.unavailable = False

            await asyncio.wait_for(task, 10)
            self.assertEqual(["job:runtime", "job:calculate"], delivered)
            self.assertEqual(["runtime", "calculate:2"], (root / "science/calls").read_text().splitlines())
            self.assertEqual((), service.claims.live_owners())
            self.assertTrue(service.shutdown_ready)
            self.assertEqual([], jobs.announcements[-1])
        finally:
            stopping.set()

            if not task.done():
                await asyncio.wait_for(task, 10)
