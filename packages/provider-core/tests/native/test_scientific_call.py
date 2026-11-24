"""Typed calls own their resources and retry only authorized, settled work."""

from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
import time
from dataclasses import dataclass
from threading import Event
import unittest
from unittest.mock import patch

from provider_core.batch_call import BatchCall, BatchPolicy
from provider_core.execution_setup import ExecutionSettings
from provider_core.hosting import AdmissionCode, CapacityUnavailable, NotAdmitted, settle_cleanup
from provider_core.payload import JsonDataclassCodec
from provider_core.scientific import Analysis, Contribution, operation
from provider_core.scientific_call import AdmissionRejected
from provider_core.runner_session import RunnerSession


@dataclass
class Sample:
    count: int


class UnreadableResultCodec(JsonDataclassCodec):
    def decode(self, payload):
        raise ValueError(f"Cannot decode result using {self.format=}")


class CalculationAnalysis(Analysis):
    def close(self) -> None:
        self.contribution.closed_analyses += 1


class Calculator(Contribution):
    analysis_type = CalculationAnalysis

    def __init__(self):
        super().__init__()

        self.inputs = []
        self.closed_analyses = 0
        self.failures_remaining = 0
        self.failure = OSError("Scientific calculation failed")
        self.entered = Event()
        self.release = Event()

        self.release.set()

    @operation(retryable_failures=(OSError,))
    def calculate(self, *, sample: Sample) -> Sample:
        self.inputs.append(sample)
        self.entered.set()

        if not (released := self.release.wait(timeout=5)):
            raise TimeoutError(f"{released=} should become true before the test allowance expires")

        if not (self.failures_remaining == 0):
            self.failures_remaining -= 1

            raise self.failure

        return Sample(count=sample.count + 1)

    @operation(result_codec=UnreadableResultCodec(format="test.unreadable.v1", value_type=Sample))
    def unreadable(self, *, sample: Sample) -> Sample:
        self.inputs.append(sample)

        return sample


class ScientificCallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.configuration = {
            "backend": "thread",
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
                "execution_stop_request_seconds": 5,
                "cleanup_overdue_flag_seconds": 5,
                "reject_encoded_input_over_bytes": 4096,
                "reject_encoded_result_over_bytes": 4096,
            },
            "resource_requirement": [],
        }

        settings = ExecutionSettings(self.configuration)
        self.host = await self.enterAsyncContext(settings.create_host())
        self.science = Calculator()
        self.hosted = self.host.adopt(contribution=self.science, requirements=frozenset())

        self.addCleanup(self.science.release.set)

    def call(self, *, error_policy, method):
        self.configuration["error_policy"] = error_policy

        return ExecutionSettings(self.configuration).bind_call(host=self.host, contribution=self.hosted, method=method)

    def batches(self, calculate):
        return BatchCall(
            call=calculate,
            policy=BatchPolicy(
                maximum_entries=1,
                collection_seconds=0.01,
                maximum_queued_input_bytes=1024,
                maximum_entry_input_bytes=1024,
            ),
            entry_codec=JsonDataclassCodec(format="test.sample.v1", value_type=Sample),
            pack=lambda participants: participants[0],
            unpack=lambda prediction: (prediction,),
        )

    async def test_thread_call_refuses_process_stop_escalation_before_science(self):
        self.configuration["operation_policy"]["execution_stop_grace_seconds"] = 1

        with self.assertRaisesRegex(ValueError, "owning ProcessCall"):
            self.call(error_policy={}, method=Calculator.calculate)

        self.assertEqual([], self.science.inputs)

    async def test_borrowed_call_cannot_grant_batch_retirement_of_shared_host(self):
        running_call = self.call(error_policy={}, method=Calculator.calculate)
        borrowed_call = self.call(error_policy={}, method=Calculator.calculate)

        self.science.release.clear()
        direct = asyncio.create_task(running_call(scientific_input=Sample(count=1)))

        try:
            self.assertTrue(await asyncio.to_thread(self.science.entered.wait, 2))

            with self.assertRaises(TypeError):
                self.batches(borrowed_call)

            self.assertFalse(direct.done())
        finally:
            self.science.release.set()
            self.assertEqual(Sample(count=2), await direct)

        self.assertEqual(Sample(count=4), await borrowed_call(scientific_input=Sample(count=3)))

    async def test_success_releases_results_and_analyses_between_calls(self):
        calculate = self.call(error_policy={}, method=Calculator.calculate)

        for count in range(3):
            prediction = await calculate(scientific_input=Sample(count=count))

            self.assertEqual(first=Sample(count=count + 1), second=prediction)
            self.assertEqual(first=count + 1, second=self.science.closed_analyses)
            self.assertEqual(first=0, second=self.host.result_reserved)

    async def test_default_raises_original_scientific_failure_after_cleanup(self):
        self.science.failures_remaining = 1
        calculate = self.call(error_policy={}, method=Calculator.calculate)

        with self.assertRaises(expected_exception=OSError) as raised:
            await calculate(scientific_input=Sample(count=1))

        self.assertIs(expr1=self.science.failure, expr2=raised.exception)
        self.assertEqual(first=1, second=self.science.closed_analyses)
        self.assertEqual(first=[Sample(count=1)], second=self.science.inputs)

    async def test_admission_rejection_retains_structured_reason(self):
        calculate = self.call(error_policy={"max_attempts": 3}, method=Calculator.calculate)
        refusal = NotAdmitted(AdmissionCode.INVALID_REQUEST, "Request does not match the hosted binding")
        analysis = self.host.open_analysis(allocations=frozenset({self.hosted.allocation}))

        with (
            patch.object(target=self.host, attribute="open_analysis", return_value=analysis),
            patch.object(target=analysis, attribute="submit", return_value=refusal),
            patch(target="provider_core.scientific_call.sleep") as backoff,
        ):
            with self.assertRaises(expected_exception=AdmissionRejected) as raised:
                await calculate(scientific_input=Sample(count=1))

            backoff.assert_not_called()

        self.assertIs(expr1=refusal, expr2=raised.exception.refusal)
        self.assertIn(member=refusal.message, container=str(raised.exception))
        self.assertEqual(first=[], second=self.science.inputs)
        self.assertEqual(first={}, second=self.host.analyses)

    async def test_capacity_retry_releases_scope_before_backoff_then_succeeds(self):
        calculate = self.call(error_policy={"max_attempts": 3}, method=Calculator.calculate)
        original_open = self.host.open_analysis
        admission_attempts = []
        delays = []

        def refuse_once(*, allocations, retain_results):
            analysis = original_open(allocations=allocations, retain_results=retain_results)

            admission_attempts.append(analysis)

            if len(admission_attempts) == 1:
                analysis.submit = lambda invocation: NotAdmitted(AdmissionCode.CAPACITY, "Result capacity is occupied")

            return analysis

        async def backoff(seconds):
            self.assertEqual(first={}, second=self.host.analyses)
            delays.append(seconds)

        with (
            patch.object(target=self.host, attribute="open_analysis", side_effect=refuse_once),
            patch(target="provider_core.scientific_call.sleep", side_effect=backoff),
            self.assertLogs(logger="provider_core.scientific_call", level="WARNING"),
        ):
            prediction = await calculate(scientific_input=Sample(count=1))

        self.assertEqual(first=Sample(count=2), second=prediction)
        self.assertEqual(first=[0.1], second=delays)
        self.assertEqual(first=[Sample(count=1)], second=self.science.inputs)

    async def test_retry_exhaustion_caps_backoff_and_preserves_last_failure(self):
        calculate = self.call(
            error_policy={"max_attempts": 4, "backoff_initial_seconds": 0.1, "backoff_max_seconds": 0.25},
            method=Calculator.calculate,
        )

        occupied = CapacityUnavailable("Another analysis owns the allocation")

        with (
            patch.object(target=self.host, attribute="open_analysis", side_effect=occupied) as acquire,
            patch(target="provider_core.scientific_call.sleep") as backoff,
            self.assertLogs(logger="provider_core.scientific_call", level="WARNING"),
        ):
            with self.assertRaises(expected_exception=CapacityUnavailable) as raised:
                await calculate(scientific_input=Sample(count=1))

        self.assertIs(expr1=occupied, expr2=raised.exception)
        self.assertEqual(first=4, second=acquire.call_count)
        self.assertEqual(first=[0.1, 0.2, 0.25], second=[call.args[0] for call in backoff.await_args_list])

    async def test_permission_withdrawn_during_backoff_prevents_the_next_attempt(self):
        calculate = self.call(error_policy={"max_attempts": 3}, method=Calculator.calculate)
        refusal = PermissionError("The API job was cancelled")
        permitted = True

        def require_execution():
            if not permitted:
                raise refusal

        async def revoke_during_backoff(seconds):
            nonlocal permitted

            permitted = False

        with (
            patch.object(target=self.host, attribute="open_analysis", side_effect=CapacityUnavailable("Busy")) as acquire,
            patch(target="provider_core.scientific_call.sleep", side_effect=revoke_during_backoff),
            self.assertLogs(logger="provider_core.scientific_call", level="WARNING"),
            self.assertRaises(expected_exception=PermissionError) as raised,
        ):
            await calculate(scientific_input=Sample(count=1), require_execution=require_execution)

        self.assertIs(expr1=refusal, expr2=raised.exception)
        self.assertEqual(first=1, second=acquire.call_count)
        self.assertEqual(first=[], second=self.science.inputs)

    async def test_scientific_retry_requires_explicit_authorization(self):
        self.science.failures_remaining = 1
        calculate = self.call(error_policy={"max_attempts": 3}, method=Calculator.calculate)

        with self.assertRaises(expected_exception=OSError):
            await calculate(scientific_input=Sample(count=1))

        self.assertEqual(first=1, second=len(self.science.inputs))
        self.science.failures_remaining = 1
        calculate = self.call(error_policy={"max_attempts": 3, "retry_scientific_failures": True}, method=Calculator.calculate)
        scientific_input = Sample(count=2)

        async def backoff(seconds):
            self.assertEqual(first=2, second=self.science.closed_analyses)
            scientific_input.count = 99

        with (
            patch(target="provider_core.scientific_call.sleep", side_effect=backoff),
            self.assertLogs(logger="provider_core.scientific_call", level="WARNING"),
        ):
            prediction = await calculate(scientific_input=scientific_input)

        self.assertEqual(first=Sample(count=3), second=prediction)
        self.assertEqual(first=[Sample(count=1), Sample(count=2), Sample(count=2)], second=self.science.inputs)

    async def test_scientific_retry_refuses_undeclared_failures_and_subclasses(self):
        for failure in (ValueError("Invalid calculation input"), PermissionError("Model access refused")):
            with self.subTest(failure_type=type(failure)):
                self.science.failure = failure
                self.science.failures_remaining = 1
                self.science.inputs.clear()
                calculate = self.call(
                    error_policy={"max_attempts": 3, "retry_scientific_failures": True},
                    method=Calculator.calculate,
                )

                with self.assertRaises(expected_exception=type(failure)) as caught:
                    await calculate(scientific_input=Sample(count=1))

                self.assertIs(expr1=caught.exception, expr2=failure)
                self.assertEqual(first=self.science.inputs, second=[Sample(count=1)])

    async def test_lost_submission_reply_does_not_replay_science(self):
        calculate = self.call(error_policy={"max_attempts": 3, "retry_scientific_failures": True}, method=Calculator.calculate)
        original_open = self.host.open_analysis
        lost_reply = OSError("Submission reply was lost after admission")

        def open_analysis(*, allocations, retain_results):
            analysis = original_open(allocations=allocations, retain_results=retain_results)
            original_submit = analysis.submit

            def lose_reply(*, invocation):
                original_submit(invocation)

                raise lost_reply

            analysis.submit = lose_reply

            return analysis

        with patch.object(target=self.host, attribute="open_analysis", side_effect=open_analysis):
            with self.assertRaises(expected_exception=OSError) as raised:
                await calculate(scientific_input=Sample(count=1))

        self.assertIs(expr1=lost_reply, expr2=raised.exception)
        self.assertLessEqual(a=len(self.science.inputs), b=1)
        self.assertEqual(first={}, second=self.host.analyses)

    async def test_decoder_failure_releases_result_without_repeating_science(self):
        calculate = self.call(error_policy={"max_attempts": 3, "retry_scientific_failures": True}, method=Calculator.unreadable)

        with self.assertRaises(expected_exception=ValueError):
            await calculate(scientific_input=Sample(count=1))

        self.assertEqual(first=[Sample(count=1)], second=self.science.inputs)
        self.assertEqual(first=1, second=self.science.closed_analyses)
        self.assertEqual(first=0, second=self.host.result_reserved)

    async def test_cleanup_failure_prevents_retry_and_keeps_both_failures(self):
        calculate = self.call(error_policy={"max_attempts": 3, "retry_scientific_failures": True}, method=Calculator.calculate)
        self.science.failures_remaining = 1
        cleanup_failure = OSError("Analysis cleanup failed")

        async def fail_cleanup(*, control):
            await settle_cleanup(control=control)

            raise cleanup_failure

        with patch(target="provider_core.scientific_call.settle_cleanup", side_effect=fail_cleanup):
            with self.assertRaises(expected_exception=ExceptionGroup) as raised:
                await calculate(scientific_input=Sample(count=1))

        self.assertEqual(first=(self.science.failure, cleanup_failure), second=raised.exception.exceptions)
        self.assertEqual(first=1, second=len(self.science.inputs))

    async def test_cancellation_waits_for_running_science_and_cleanup_without_retry(self):
        calculate = self.call(error_policy={"max_attempts": 3, "retry_scientific_failures": True}, method=Calculator.calculate)

        self.science.release.clear()

        invocation_task = asyncio.create_task(coro=calculate(scientific_input=Sample(count=1)))

        try:
            self.assertTrue(expr=await asyncio.to_thread(self.science.entered.wait, timeout=2))
            invocation_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(expr=invocation_task.done())
        finally:
            self.science.release.set()

        with self.assertRaises(expected_exception=asyncio.CancelledError):
            await invocation_task

        self.assertEqual(first=1, second=self.science.closed_analyses)
        self.assertEqual(first=[Sample(count=1)], second=self.science.inputs)
        self.assertEqual(first=0, second=self.host.result_reserved)

    async def test_receiver_failure_is_not_retried_or_converted_to_science_failure(self):
        calculate = self.call(error_policy={"max_attempts": 3, "retry_scientific_failures": True}, method=Calculator.calculate)
        receiver_failure = OSError("Receiver acknowledgement was lost")
        received = []

        async def receive(payload):
            received.append(payload)

            raise receiver_failure

        with self.assertRaises(expected_exception=OSError) as raised:
            await calculate(scientific_input=Sample(count=1), receive=receive)

        self.assertIs(expr1=receiver_failure, expr2=raised.exception)
        self.assertEqual(first=1, second=len(received))
        self.assertEqual(first=1, second=len(self.science.inputs))
        self.assertEqual(first=0, second=self.host.result_reserved)

    async def test_cancelled_receiver_is_joined_before_scope_cleanup(self):
        calculate = self.call(error_policy={}, method=Calculator.calculate)
        receiver_entered = asyncio.Event()
        release_receiver = asyncio.Event()
        received = []

        async def receive(payload):
            receiver_entered.set()
            await release_receiver.wait()
            received.append(payload)

        invocation_task = asyncio.create_task(coro=calculate(scientific_input=Sample(count=1), receive=receive))

        try:
            await receiver_entered.wait()
            invocation_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(expr=invocation_task.done())
            self.assertEqual(first=0, second=self.science.closed_analyses)
        finally:
            release_receiver.set()

        with self.assertRaises(expected_exception=asyncio.CancelledError):
            await invocation_task

        self.assertEqual(first=1, second=len(received))
        self.assertEqual(first=1, second=self.science.closed_analyses)
        self.assertEqual(first=0, second=self.host.result_reserved)

    async def test_cancellation_during_backoff_stops_attempts(self):
        calculate = self.call(error_policy={"max_attempts": 3}, method=Calculator.calculate)

        with (
            patch.object(target=self.host, attribute="open_analysis", side_effect=CapacityUnavailable("Allocation occupied")) as acquire,
            patch(target="provider_core.scientific_call.sleep", side_effect=asyncio.CancelledError),
            self.assertLogs(logger="provider_core.scientific_call", level="WARNING"),
        ):
            with self.assertRaises(expected_exception=asyncio.CancelledError):
                await calculate(scientific_input=Sample(count=1))

        self.assertEqual(first=1, second=acquire.call_count)

    async def test_synchronous_admission_cannot_dispatch_after_the_deadline(self):
        calculate = self.call(error_policy={"total_seconds": 0.02}, method=Calculator.calculate)
        original_open = self.host.open_analysis
        analyses = []

        def slow_open(**options):
            analysis = original_open(**options)

            analyses.append(analysis)
            time.sleep(0.03)

            return analysis

        with (
            patch.object(target=self.host, attribute="open_analysis", side_effect=slow_open),
            patch(target="provider_core.scientific_call.Submission.submit") as submit,
            self.assertRaises(expected_exception=TimeoutError),
        ):
            await calculate(scientific_input=Sample(count=1))

        submit.assert_not_called()
        self.assertEqual(first=1, second=len(analyses))
        self.assertEqual(first={}, second=self.host.analyses)
        self.assertEqual(first=[], second=self.science.inputs)

    async def test_budget_spans_backoffs_instead_of_restarting_for_each_attempt(self):
        calculate = self.call(
            error_policy={"max_attempts": 4, "total_seconds": 0.3},
            method=Calculator.calculate,
        )
        delays = []

        async def backoff(seconds):
            delays.append(seconds)

            await asyncio.sleep(0.2)

        with (
            patch.object(target=self.host, attribute="open_analysis", side_effect=CapacityUnavailable("Occupied")) as acquire,
            patch(target="provider_core.scientific_call.sleep", side_effect=backoff),
            self.assertLogs(logger="provider_core.scientific_call", level="WARNING"),
            self.assertRaises(expected_exception=TimeoutError),
        ):
            await calculate(scientific_input=Sample(count=1))

        self.assertEqual(first=2, second=acquire.call_count)
        self.assertEqual(first=[0.1, 0.2], second=delays)
        self.assertEqual(first=[], second=self.science.inputs)

    async def test_expiration_waits_for_running_science_and_cleanup_without_retry(self):
        calculate = self.call(
            error_policy={"max_attempts": 3, "retry_scientific_failures": True, "total_seconds": 0.1},
            method=Calculator.calculate,
        )

        self.science.release.clear()

        invocation = asyncio.create_task(coro=calculate(scientific_input=Sample(count=1)))

        try:
            self.assertTrue(expr=await asyncio.to_thread(self.science.entered.wait, 2))

            await asyncio.sleep(0.15)

            self.assertFalse(expr=invocation.done())
            self.assertEqual(first=0, second=self.science.closed_analyses)
        finally:
            self.science.release.set()

        with self.assertRaises(expected_exception=TimeoutError):
            await invocation

        self.assertEqual(first=1, second=self.science.closed_analyses)
        self.assertEqual(first=1, second=len(self.science.inputs))
        self.assertEqual(first=0, second=self.host.result_reserved)

    def test_invalid_recovery_settings_fail_before_scientific_work(self):
        for settings in (
            {"total_seconds": 0},
            {"total_seconds": float("inf")},
            {"total_seconds": True},
            {"max_attempts": 0},
            {"max_attempts": True},
            {"backoff_initial_seconds": 0},
            {"backoff_max_seconds": float("inf")},
            {"backoff_initial_seconds": 2, "backoff_max_seconds": 1},
            {"retry_admission_codes": ["unknown"]},
            {"retry_scientific_failures": "yes"},
            {"unknown_setting": True},
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(expected_exception=(TypeError, ValueError)):
                    self.call(error_policy=settings, method=Calculator.calculate)

        self.assertEqual(first=[], second=self.science.inputs)


class RunnerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.lease_path = self.directory / "lease"

        self.lease_path.touch(mode=0o600)

        self.runner = {
            "socket": str(self.directory / "runner.sock"),
            "lease_file": str(self.lease_path),
            "role": "science",
            "startup_seconds": 2,
            "retirement_seconds": 2,
        }

    def test_session_uses_the_checked_lease_identity_without_connecting(self):
        session = RunnerSession.from_config(self.runner)

        self.addCleanup(session.close)

        lease = self.lease_path.stat()

        self.assertEqual(first=(lease.st_dev, lease.st_ino), second=session.expected_lock_identity)
        self.assertFalse(expr=session.control_connected)

    def test_unsafe_lease_is_rejected_before_session_construction(self):
        self.lease_path.chmod(0o644)

        with self.assertRaises(expected_exception=PermissionError):
            RunnerSession.from_config(self.runner)

        self.lease_path.unlink()
        self.lease_path.symlink_to(self.directory)

        with self.assertRaises(expected_exception=OSError):
            RunnerSession.from_config(self.runner)

        self.lease_path.unlink()
        os.mkfifo(self.lease_path, mode=0o600)

        with self.assertRaises(expected_exception=PermissionError):
            RunnerSession.from_config(self.runner)

    def test_failed_host_construction_closes_the_unconnected_session(self):
        session = RunnerSession.from_config(self.runner)

        settings = ExecutionSettings(
            {
                "backend": "process",
                "host": {"unknown_host_option": True},
                "operation_policy": {
                    "queue_stop_request_seconds": 2,
                    "execution_stop_request_seconds": 2,
                    "cleanup_overdue_flag_seconds": 2,
                    "reject_encoded_input_over_bytes": 1024,
                    "reject_encoded_result_over_bytes": 1024,
                },
                "resource_requirement": [],
            },
        )

        async def open_call():
            async with settings.open_process_call(
                runner=self.runner,
                contribution_type=Calculator,
                constructor=Calculator,
                startup={},
                method=Calculator.calculate,
            ):
                self.fail(msg="Invalid host options must fail before opening a call")

        with patch.object(target=RunnerSession, attribute="from_config", return_value=session):
            with self.assertRaises(expected_exception=TypeError):
                asyncio.run(open_call())

        self.assertEqual(first=-1, second=session.control.fileno())
