"""Keep real scientific work original across delivery, waiting and result lifetime."""

import asyncio
from dataclasses import dataclass
from threading import Event
import unittest
from unittest.mock import patch

from provider_core.embedded_host import EmbeddedHost
from provider_core.hosting import AdmissionCode, NotAdmitted, OperationPolicy, ResultState, observe_cleanup
from provider_core.scientific import Contribution, operation
from provider_core.submission import Submission


@dataclass(frozen=True, slots=True, kw_only=True)
class Sample:
    value: int


class Science(Contribution):
    def __init__(self):
        super().__init__()
        self.inputs = []
        self.failure = None
        self.entered = None
        self.release = None

    @operation()
    def calculate(self, *, sample: Sample) -> Sample:
        self.inputs.append(sample)
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("Test did not release scientific calculation")
        if self.failure is not None:
            raise self.failure
        return sample


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = await self.enterAsyncContext(EmbeddedHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        ))
        self.science = Science()
        self.hosted = self.host.adopt(contribution=self.science, requirements=frozenset())
        self.execution = self.host.open_analysis(allocations=frozenset({self.hosted.allocation}))
        self.policy = OperationPolicy(
            queue_stop_request_seconds=5,
            execution_stop_request_seconds=5,
            cleanup_overdue_flag_seconds=5,
            reject_encoded_input_over_bytes=4096,
            reject_encoded_result_over_bytes=4096,
        )

    def submission(self, *, value):
        return Submission.from_call(
            execution=self.execution,
            call=self.hosted.call(method=self.science.calculate),
            scientific_input=Sample(value=value),
            states=(),
            reservation=self.execution.reservation(allocation=self.hosted.allocation),
            policy=self.policy,
        )

    async def test_construction_and_wait_do_not_submit(self):
        submit = self.execution.submit
        with patch.object(target=self.execution, attribute="submit", wraps=submit) as deliver:
            submission = self.submission(value=1)
            with self.assertRaises(expected_exception=RuntimeError):
                await submission.wait()
            deliver.assert_not_called()
        self.assertIsNone(obj=submission.operation)
        self.assertEqual(first=[], second=self.science.inputs)

    async def test_lost_reply_reuses_the_exact_invocation_and_original_operation(self):
        submission = self.submission(value=1)
        original = OSError("The scientific submission reply was lost")
        submit = self.execution.submit
        requests = []
        operations = []

        def lose_reply(*, invocation):
            requests.append(invocation)
            admitted = submit(invocation=invocation)
            operations.append(admitted)
            if len(requests) == 1:
                raise original
            return admitted

        with patch.object(target=self.execution, attribute="submit", new=lose_reply):
            with self.assertRaises(expected_exception=OSError) as raised:
                submission.submit()
            self.assertIs(expr1=original, expr2=raised.exception)
            self.assertIsNone(obj=submission.operation)
            admitted = submission.submit()
            self.assertIs(expr1=admitted, expr2=submission.submit())
            completed = await submission.wait()
        self.assertIs(expr1=requests[0], expr2=requests[1])
        self.assertIs(expr1=operations[0], expr2=operations[1])
        self.assertEqual(first=2, second=len(requests))
        completed.require_result()
        self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)

    async def test_refusal_requires_explicit_retry_and_does_not_acquire_work(self):
        first = self.submission(value=1)
        first.submit()
        completed = await first.wait()
        second = self.submission(value=2)
        refused = second.submit()
        self.assertIsInstance(obj=refused, cls=NotAdmitted)
        self.assertIs(expr1=AdmissionCode.CAPACITY, expr2=refused.code)
        with self.assertRaises(expected_exception=RuntimeError):
            await second.wait()
        self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)
        await observe_cleanup(control=first.operation.release_result(result=completed.require_result()))
        second.submit()
        (await second.wait()).require_result()
        self.assertEqual(first=[Sample(value=1), Sample(value=2)], second=self.science.inputs)

    async def test_canceled_wait_does_not_stop_or_replay_science(self):
        self.science.entered = Event()
        self.science.release = Event()
        submission = self.submission(value=1)
        admitted = submission.submit()
        waiter = asyncio.create_task(coro=submission.wait())
        try:
            self.assertTrue(expr=await asyncio.to_thread(self.science.entered.wait, timeout=2))
            waiter.cancel()
            with self.assertRaises(expected_exception=asyncio.CancelledError):
                await waiter
            self.science.release.set()
            (await submission.wait()).require_result()
            self.assertIs(expr1=admitted, expr2=submission.submit())
            self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)
        finally:
            self.science.release.set()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_acquired_operation_survives_analysis_close_without_submission(self):
        submission = self.submission(value=1)
        admitted = submission.submit()
        (await submission.wait()).require_result()
        await observe_cleanup(control=self.execution.close())
        with patch.object(target=self.execution, attribute="submit", side_effect=AssertionError("Closed session must not receive work")):
            self.assertIs(expr1=admitted, expr2=submission.submit())
            completed = await submission.wait()
        completed.require_result()
        self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)

    async def test_wait_after_release_observes_current_result_state(self):
        submission = self.submission(value=1)
        submission.submit()
        completed = await submission.wait()
        await observe_cleanup(control=submission.operation.release_result(result=completed.require_result()))
        current = await submission.wait()
        self.assertIs(expr1=ResultState.RELEASED, expr2=current.result_state)
        with self.assertRaises(expected_exception=RuntimeError):
            current.require_result()

    async def test_scientific_failure_keeps_original_cause(self):
        original = OSError("Model calculation failed")
        self.science.failure = original
        submission = self.submission(value=1)
        submission.submit()
        completed = await submission.wait()
        with self.assertRaises(expected_exception=OSError) as raised:
            completed.require_result()
        self.assertIs(expr1=original, expr2=raised.exception)
