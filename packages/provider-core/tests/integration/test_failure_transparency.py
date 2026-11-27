"""Provider-independent evidence tests through installed SDK workers and Attempts.

Only external endpoint/API/delivery replies are scripted. Interpretation,
scientific execution, process framing, retirement and reporting use the SDK.
The release qualifier discovers this suite in the candidate source archive and
runs it against the installed wheel; no scientific provider is imported.
"""

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from provider_core.analysis import AnalysisSuccess, PreparedAnalysis
from provider_core.payload import EncodedPayload
from tests.fakes.outcomes import BytesCodec
from provider_core.attempt_record import ProtectedEvidence
from provider_core.chat import ChatFailed
from provider_core.execution_setup import ExecutionSettings
from provider_core.interpreter_settings import ManagedInterpreter
from provider_core.job_service import HostFailurePolicy, JobOffering, JobService, JobServicePolicy
from provider_core.ownership import LiveAttemptLimits
from provider_core.process_wire import (
    MAX_DIAGNOSTIC_CHARACTERS,
    MAX_EXCEPTION_NOTES,
    MAX_FAILURE_NOTE_CHARACTERS,
    decoded_error_evidence,
)
from provider_core.scientific import Contribution, operation
from provider_core.validation.upstream.nmr.v1.hello import AnalysisOffering
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.conversation import endpoint, interpreter_test_limits
from tests.fakes.job_service import JobApi
from tests.fakes.runner import RunningSupervisor


SECRET = "fixture-opaque-credential"
PRIVATE_INPUT = "fixture-private-spectrum-values"


@dataclass
class ScientificInput:
    specification: str


@dataclass
class ScientificResult:
    process_id: int


class LaterCalculationFailed(RuntimeError):
    """A partial scientific effect is deliberately ineligible for replay."""


class Science(Contribution):
    def __init__(self, *, startup):
        super().__init__()
        self.witness = Path(startup["witness_directory"])
        self.scenario = startup["scenario"]

    @operation(retryable_failures=(OSError,))
    def calculate(self, *, request: ScientificInput) -> ScientificResult:
        with (self.witness / "calls").open("a") as stream:
            stream.write(str(os.getpid()) + "\n")

        if self.scenario == "retry":
            if len((self.witness / "calls").read_text().splitlines()) == 1:
                failure = OSError("First calculation failed before any result")
                failure.add_note("retryable=False; this note is evidence, not retry authority")
                raise failure

            return ScientificResult(process_id=os.getpid())

        completed = []
        for index in range(2):
            with (self.witness / "items").open("a") as stream:
                stream.write(str(index) + "\n")
            if index == 0:
                completed.append(sha256(request.specification.encode()).hexdigest())
                (self.witness / "completed").write_text(json.dumps(completed))
                continue

            try:
                raise ValueError("Scientific cause; " + request.specification)
            except ValueError as cause:
                failure = LaterCalculationFailed("Later calculation failed; " + SECRET)
                failure.add_note(f"failed_index={index}; completed_request_sha256={completed}; artifact_id=fixture-result")
                failure.add_note("retryable=True; this note cannot authorize replay")
                failure.add_note("OVERSIZED-NOTE-MUST-BE-OMITTED" + "x" * MAX_DIAGNOSTIC_CHARACTERS)
                for observation in range(MAX_EXCEPTION_NOTES):
                    failure.add_note(f"additional observation={observation}")
                raise failure from cause



def construct(*, startup):
    return Science(startup=startup)


class Session:
    """Supply scientific behavior while the SDK owns all Attempt/reporting work."""

    def __init__(self, call, scenario):
        self.call = call
        self.scenario = scenario
        self.interpreter = None
        self.failure = None
        self.retire_calls = 0
        self.cleanup_failure = OSError("Fixture cleanup did not complete")
        self.cleanup_failure.add_note("independent cleanup observation")

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.input = ScientificInput(instructions.specification_bytes.decode())
        if self.scenario == "interpretation":
            self.endpoint = endpoint("fixture", ChatFailed("http_status", "Endpoint refused " + SECRET))
            limits = interpreter_test_limits() | {"total_http_attempts_per_turn": 1}
            factory = Mock(return_value=self.endpoint, context_bytes=32768)
            self.interpreter = ManagedInterpreter(
                (factory,),
                interpretation_budget_seconds=5,
                reject_combined_messages_and_tools_over_bytes=32768,
                secrets=(SECRET,),
                **limits,
            )
            try:
                await self.interpreter.run(
                    messages=[{"role": "user", "content": self.input.specification}],
                    tools=[],
                    dispatch=self.unused_dispatch,
                )
            except Exception as cause:
                self.failure = cause
                raise

        return PreparedAnalysis(ProtectedEvidence("fixture.input_digest.v1", sha256(instructions.specification_bytes).hexdigest().encode()))

    async def unused_dispatch(self, *args, **kwargs):
        raise AssertionError("The failed endpoint must not dispatch a tool")

    async def execute(self, prepared, *, report_condition):
        try:
            result = await self.call(scientific_input=self.input, require_execution=report_condition.require_execution)
        except Exception as cause:
            self.failure = cause
            raise
        content = str(result.process_id).encode()
        return AnalysisSuccess(
            result=EncodedPayload(
                codec=BytesCodec(format="fixture.result.v1"),
                value=content,
            ),
            evidence=ProtectedEvidence("fixture.result.v1", content),
        )

    async def retire(self):
        self.retire_calls += 1
        if self.interpreter is not None:
            await self.interpreter.retire()
        if (self.scenario == "cleanup") and (self.retire_calls == 1):
            raise self.cleanup_failure


class FailureTransparencyTests(unittest.IsolatedAsyncioTestCase):
    async def start_science(self, scenario):
        witness = Path(self.enterContext(TemporaryDirectory()))
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
                "error_policy": {
                    "max_attempts": 2,
                    "retry_scientific_failures": True,
                    "backoff_initial_seconds": 0.01,
                    "backoff_max_seconds": 0.01,
                },
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
            startup={"witness_directory": str(witness), "scenario": scenario},
            method=Science.calculate,
        )
        self.addAsyncCleanup(call.close)
        await call.start()
        return (witness, call)

    async def run_failed_job(self, scenario, *, reporting_error=None):
        (witness, call) = await self.start_science(scenario)
        api = JobApi()
        api.jobs = {"job:transparency": ("fixture", PRIVATE_INPUT.encode())}
        session = Session(call, scenario)
        delivery = RecordingDelivery()
        service = JobService(
            provider_ref="provider:example",
            offerings=(JobOffering(AnalysisOffering("fixture", "Diagnostic fixture"), call.host, "fixture", "one", lambda: session),),
            host_failure_policy=HostFailurePolicy.STOP,
            limits=LiveAttemptLimits(1, 262144, 131072),
            display_name="SDK failure fixture",
            description="No scientific provider dependency",
            policy=JobServicePolicy(0.01, 10, 0.05, 1, 0.01, 1),
            new_api=api.new_api,
            new_delivery=lambda: delivery,
            stopping=asyncio.Event(),
            diagnostic_secrets=(SECRET, PRIVATE_INPUT),
        )
        records = []

        class Reports(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
                if reporting_error is not None:
                    raise reporting_error

        logger = logging.getLogger("provider_core.job_service")
        handler = Reports(level=logging.ERROR)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        with self.assertRaises(Exception) as caught:
            await asyncio.wait_for(service.serve(), timeout=15)

        self.assertTrue(expr=service.shutdown_ready)
        self.assertEqual(first=[], second=delivery.delivery_outcomes)
        self.assertGreaterEqual(a=session.retire_calls, b=1)
        (controller,) = service.claims.live_owners()
        self.assertIs(expr1=session.failure, expr2=controller.last_failure.cause.cause)
        if isinstance(caught.exception, BaseExceptionGroup):
            self.assertIsNotNone(obj=caught.exception.subgroup(lambda failure: failure is controller.last_failure))
        else:
            self.assertIs(expr1=controller.last_failure, expr2=caught.exception)
        report = "\n".join(records)
        self.assertIn(member="job:transparency", container=report)
        self.assertIn(member=next(iter(api.attempts)), container=report)
        self.assertNotIn(member=SECRET, container=report)
        self.assertNotIn(member=PRIVATE_INPUT, container=report)
        return (witness, session, controller, caught.exception, report)

    async def test_interpretation_failure_reaches_attempt_report_without_starting_science(self):
        (witness, session, _, _, report) = await self.run_failed_job("interpretation")
        self.assertIn(member="InterpretationFailed", container=report)
        self.assertIn(member="ChatFailed", container=report)
        self.assertIn(member="Endpoint refused", container=report)
        self.assertIn(member="[redacted]", container=report)
        self.assertFalse(expr=(witness / "calls").exists())
        self.assertTrue(expr=session.interpreter.retirement_confirmed)
        session.endpoint.retire.assert_awaited()

    async def test_later_worker_failure_retains_completed_evidence_without_replay(self):
        (witness, session, _, _, report) = await self.run_failed_job("later")
        self.assertIn(member="LaterCalculationFailed", container=report)
        self.assertIn(member="ValueError", container=report)
        self.assertIn(member="remote_frames", container=report)
        self.assertIn(member="failed_index=1", container=report)
        (completed_digest,) = json.loads((witness / "completed").read_text())
        self.assertIn(member=completed_digest, container=report)
        self.assertEqual(first=["0", "1"], second=(witness / "items").read_text().splitlines())
        self.assertIn(member="artifact_id=fixture-result", container=report)
        self.assertIn(member="notes_omitted", container=report)
        evidence = decoded_error_evidence(session.failure)
        self.assertTrue(expr=evidence.notes_omitted)
        self.assertLessEqual(a=len(evidence.notes), b=MAX_EXCEPTION_NOTES)
        self.assertTrue(expr=all(len(note) <= MAX_DIAGNOSTIC_CHARACTERS for note in evidence.notes))
        self.assertLessEqual(a=sum(map(len, evidence.notes)), b=MAX_FAILURE_NOTE_CHARACTERS)
        self.assertNotIn(member="OVERSIZED-NOTE-MUST-BE-OMITTED", container="".join(evidence.notes))
        self.assertNotIn(member="OVERSIZED-NOTE-MUST-BE-OMITTED", container=report)
        (child_pid,) = (witness / "calls").read_text().splitlines()
        self.assertNotEqual(first=os.getpid(), second=int(child_pid))

    async def test_independent_cleanup_failure_does_not_replace_worker_evidence(self):
        (witness, session, controller, _, report) = await self.run_failed_job("cleanup")
        self.assertIn(member="LaterCalculationFailed", container=report)
        self.assertIn(member="independent cleanup observation", container=report)
        self.assertIn(member=session.cleanup_failure, container=controller.last_failure.cause.cleanup_failures)
        self.assertGreaterEqual(a=session.retire_calls, b=2)
        self.assertEqual(first=1, second=len((witness / "calls").read_text().splitlines()))

    async def test_reporting_failure_retains_original_worker_failure_and_does_not_replay(self):
        reporting_error = OSError("Fixture diagnostic sink failed")
        (witness, session, controller, caught, _) = await self.run_failed_job("later", reporting_error=reporting_error)
        self.assertIsInstance(obj=caught, cls=BaseExceptionGroup)
        self.assertIsNotNone(obj=caught.subgroup(lambda error: error is reporting_error))
        self.assertIsNotNone(obj=caught.subgroup(lambda error: error is controller.last_failure))
        self.assertIs(expr1=session.failure, expr2=controller.last_failure.cause.cause)
        self.assertEqual(first=1, second=len((witness / "calls").read_text().splitlines()))

    async def test_exception_notes_do_not_disable_an_authorized_first_failure_retry(self):
        (witness, call) = await self.start_science("retry")
        with self.assertLogs(logger="provider_core.scientific_call", level="WARNING"):
            result = await call(scientific_input=ScientificInput(PRIVATE_INPUT))
        process_ids = [int(value) for value in (witness / "calls").read_text().splitlines()]
        self.assertEqual(first=2, second=len(process_ids))
        self.assertNotEqual(first=process_ids[0], second=process_ids[1])
        self.assertEqual(first=process_ids[1], second=result.process_id)
