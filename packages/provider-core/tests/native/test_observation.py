"""Prove result usability without duplicating the execution backend's state machine."""

from dataclasses import dataclass, replace
import unittest

from provider_core.embedded_host import InProcessExecutionHost

from provider_core.hosting import (
    Failure,
    NotAdmitted,
    OperationPolicy,
    Outcome,
    ResultState,
    observe_cleanup,
    observe_completion,
)

from provider_core.scientific import Contribution, operation


@dataclass(frozen=True, slots=True, kw_only=True)
class Value:
    number: int


class Echo(Contribution):
    @operation()
    def echo(self, *, value: Value) -> Value:
        return value


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = InProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )

        self.addAsyncCleanup(self.retire)
        hosted = self.host.adopt(contribution=Echo(), requirements=frozenset())
        analysis = self.host.open_analysis(allocations=frozenset({hosted.allocation}))
        (reservation,) = analysis.reservations

        self.operation = analysis.submit_method(
            method=hosted.contribution.echo,
            scientific_input=Value(number=7),
            states=(),
            reservation=reservation,
            policy=OperationPolicy(
                queue_stop_request_seconds=5,
                execution_stop_request_seconds=5,
                cleanup_overdue_flag_seconds=5,
                reject_encoded_input_over_bytes=4096,
                reject_encoded_result_over_bytes=4096,
            ),
        )

        self.assertNotIsInstance(obj=self.operation, cls=NotAdmitted)
        self.observation = await observe_completion(operation=self.operation)

    async def retire(self):
        await observe_cleanup(control=self.host.retire())

    async def test_returns_exact_handle_without_releasing_the_result(self):
        result = self.observation.require_result()

        self.assertIs(expr1=self.observation.result, expr2=result)
        self.assertEqual(first=b'{"number":7}', second=await self.operation.read_result(result=result))
        current = await self.operation.observe(after=None, wait_seconds=0)

        self.assertIs(expr1=ResultState.AVAILABLE, expr2=current.result_state)

    def test_original_failure_takes_precedence_over_available_bytes(self):
        original = OSError("Result encoding did not complete")
        failed = replace(self.observation, failures=(Failure(phase="encoding", cause=original),))

        with self.assertRaises(expected_exception=OSError) as raised:
            failed.require_result()

        self.assertIs(expr1=original, expr2=raised.exception)

    def test_independent_failures_keep_their_original_objects_and_order(self):
        original = ValueError("Science failed")
        interruption = KeyboardInterrupt("Observation was interrupted")

        failed = replace(self.observation,
            failures=(
                Failure(phase="science", cause=original),
                Failure(phase="backend", cause=interruption),
        ))

        with self.assertRaises(expected_exception=BaseExceptionGroup) as raised:
            failed.require_result()

        self.assertEqual(first=(original, interruption), second=raised.exception.exceptions)

    def test_incomplete_unsuccessful_or_unavailable_result_is_refused(self):
        changes = (
            {"finished": False},
            {"outcome": None},
            {"outcome": Outcome.REJECTED},
            {"outcome": Outcome.FAILED},
            {"result_state": ResultState.NONE},
            {"result_state": ResultState.ENCODING_FAILED},
            {"result_state": ResultState.RELEASED},
            {"result": None},
        )

        for changed in changes:
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(expected_exception=RuntimeError, expected_regex="Cannot use the result"):
                    replace(self.observation, **changed).require_result()
