"""Exercise model proposals through real scientific execution and state custody."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.contribution_errors import CallRejected
from provider_core.embedded_host import InProcessExecutionHost
from provider_core.hosting import NotAdmitted, OperationPolicy, ResultState, StartDeadlineExpired, observe_cleanup, observe_completion
from provider_core.interpreter import Interpreter
from provider_core.preparation_tool import PreparationTool
from provider_core.scientific import AssetAccess, Contribution, ScientificReturn, StateType, StateUse, operation

from tests.fakes.conversation import call, endpoint


@dataclass(frozen=True, slots=True, kw_only=True)
class Sample:
    value: int


def dispose(sample: Sample) -> None:
    pass


class Science(Contribution):
    kind = StateType(value_type=Sample)
    prepared = kind.output(dispose=dispose)
    candidate = kind.input(use=StateUse.CONSUME)

    def __init__(self):
        super().__init__()
        self.inputs = []
        self.failure = None
        self.entered = None
        self.release = None
        self.load_entered = None
        self.load_release = None
        self.initializations = 0

    def initialize(self, *, assets: AssetAccess) -> None:
        self.initializations += 1

        if self.load_entered is not None:
            self.load_entered.set()

            if not self.load_release.wait(timeout=5):
                raise TimeoutError("Test did not release scientific loading")

    @operation(state_outputs=(prepared,))
    def prepare(self, *, sample: Sample) -> ScientificReturn[Sample]:
        self.inputs.append(sample)

        if self.entered is not None:
            self.entered.set()

            if not self.release.wait(timeout=5):
                raise TimeoutError("Test did not release scientific preparation")

        if self.failure is not None:
            raise self.failure

        if sample.value < 0:
            raise CallRejected(code="negative", message="Supply a nonnegative sample")

        return ScientificReturn(value=sample, states=(self.prepared.returned(value=sample),))

    @operation(state_inputs=(candidate,))
    def consume(self, *, candidate: Sample) -> Sample:
        return candidate


class PreparationToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.execution_host = await self.enterAsyncContext(InProcessExecutionHost(
            input_bytes=4096,
            result_bytes=8192,
            state_values=2,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        ))

        self.science = Science()
        self.hosted_contribution = self.execution_host.adopt(contribution=self.science, requirements=frozenset())
        self.analysis_executor = self.execution_host.open_analysis(allocations=frozenset({self.hosted_contribution.allocation}))

        self.preparation_tool = PreparationTool(
            call_binding=self.hosted_contribution.call(method=self.science.prepare),
            state_output_slot=Science.prepared,
            analysis_executor=self.analysis_executor,
            reservation_ref=self.analysis_executor.reservation(allocation=self.hosted_contribution.allocation),
            operation_policy=OperationPolicy(
                queue_stop_request_seconds=5,
                execution_stop_request_seconds=5,
                cleanup_overdue_flag_seconds=5,
                reject_encoded_input_over_bytes=4096,
                reject_encoded_result_over_bytes=4096,
            ),
            description="Submit a nonnegative sample",
        )

    async def propose(self, *, value):
        return await self.preparation_tool.dispatch(
            name=self.preparation_tool.name,
            arguments={"value": value},
            not_after=asyncio.get_running_loop().time() + 5,
        )

    async def test_interpreter_corrects_shape_and_science_then_retains_one_state(self):
        carrier = endpoint(
            "preparation",
            call("shape", "prepare", {"value": "wrong"}),
            call("science", "prepare", {"value": -1}),
            call("accepted", "prepare", {"value": 2}),
        )

        interpreter = Interpreter(
            endpoint_factories=(lambda **settings: carrier,),
            timeout_seconds=5,
            context_bytes=32768,
            secrets=(),
        )

        self.addAsyncCleanup(interpreter.retire)
        await interpreter.run(messages=[], tools=[self.preparation_tool.definition], dispatch=self.preparation_tool.dispatch)
        self.assertEqual(first=[Sample(value=-1), Sample(value=2)], second=self.science.inputs)
        self.assertEqual(first=self.preparation_tool.operation.reference, second=self.preparation_tool.prepared_state_handle.producer)
        observation = await self.preparation_tool.operation.observe(after=None, wait_seconds=0)

        self.assertIs(expr1=ResultState.RELEASED, expr2=observation.result_state)

    async def test_operator_failure_is_original_and_cannot_restart_preparation(self):
        original = OSError("Scientific model could not read its weights")
        self.science.failure = original

        with self.assertRaises(expected_exception=OSError) as raised:
            await self.propose(value=1)

        self.assertIs(expr1=original, expr2=raised.exception)

        with self.assertRaises(expected_exception=RuntimeError):
            await self.propose(value=2)

        self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)

    async def test_encoding_rejection_is_not_a_correctable_model_proposal(self):
        original = CallRejected(code="encoding", message="Report encoding failed")
        codec = self.preparation_tool.tool_definition.call_binding.result_codec
        encode = type(codec).encode

        def fail_report(owner, value):
            if owner is codec:
                raise original

            return encode(self=owner, value=value)

        with patch.object(target=type(codec), attribute="encode", new=fail_report):
            with self.assertRaises(expected_exception=CallRejected) as raised:
                await self.propose(value=1)

        self.assertIs(expr1=original, expr2=raised.exception)
        self.assertIsNone(obj=self.preparation_tool.prepared_state_handle)
        self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)

    async def test_report_release_failure_keeps_state_and_forbids_another_proposal(self):
        original = OSError("Report release reply was lost")

        # Fault observation of the release control; keep actual preparation,
        # release request and shared cleanup observation in the exercised path.
        submit = self.analysis_executor.submit

        def failing_release(*, invocation):
            admitted = submit(invocation=invocation)
            release_result = admitted.release_result

            def release(*, result):
                release_result(result=result)

                return SimpleNamespace(observe=AsyncMock(side_effect=original))

            self.enterContext(patch.object(target=admitted, attribute="release_result", new=release))

            return admitted

        with patch.object(target=self.analysis_executor, attribute="submit", new=failing_release):
            with self.assertRaises(expected_exception=OSError) as raised:
                await self.propose(value=1)

        self.assertIs(expr1=original, expr2=raised.exception)
        self.assertIsNotNone(obj=self.preparation_tool.prepared_state_handle)

        with self.assertRaises(expected_exception=RuntimeError):
            await self.propose(value=2)

        self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)

        operation = self.analysis_executor.submit_method(
            method=self.science.consume,
            states=(Science.candidate.bind(handle=self.preparation_tool.prepared_state_handle),),
            reservation=self.preparation_tool.reservation_ref,
            policy=self.preparation_tool.operation_policy,
        )

        self.assertNotIsInstance(obj=operation, cls=NotAdmitted)
        completed = await observe_completion(operation=operation)
        result = completed.require_result()

        report = self.hosted_contribution.call(method=self.science.consume).result_codec.decode(
            payload=await operation.read_result(result=result),
        )

        self.assertEqual(first=Sample(value=1), second=report)

    async def test_canceled_wait_retains_original_operation_and_cannot_submit_again(self):
        self.science.entered = Event()
        self.science.release = Event()
        task = asyncio.create_task(coro=self.propose(value=1))

        try:
            self.assertTrue(expr=await asyncio.to_thread(self.science.entered.wait, timeout=2))
            original = self.preparation_tool.operation

            with self.assertRaises(expected_exception=RuntimeError):
                await self.propose(value=0)

            task.cancel()

            with self.assertRaises(expected_exception=asyncio.CancelledError):
                await task

            with self.assertRaises(expected_exception=RuntimeError):
                await self.propose(value=2)

            self.science.release.set()
            completed = await observe_completion(operation=original)

            completed.require_result()
            await self.preparation_tool.resume()

            with self.assertRaises(expected_exception=RuntimeError):
                await self.propose(value=3)

            self.assertIs(expr1=original, expr2=self.preparation_tool.operation)
            self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)
        finally:
            self.science.release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_lost_submission_reply_resumes_the_same_invocation(self):
        original = OSError("Preparation submission reply was lost")
        submit = self.analysis_executor.submit
        requests = []
        operations = []

        def lose_reply(*, invocation):
            requests.append(invocation)
            operation = submit(invocation=invocation)

            operations.append(operation)

            if len(requests) == 1:
                raise original

            return operation

        with patch.object(target=self.analysis_executor, attribute="submit", new=lose_reply):
            with self.assertRaises(expected_exception=OSError) as raised:
                await self.propose(value=1)

            self.assertIs(expr1=original, expr2=raised.exception)

            with self.assertRaises(expected_exception=RuntimeError):
                await self.propose(value=2)

            await self.preparation_tool.resume()

        self.assertIs(expr1=requests[0], expr2=requests[1])
        self.assertIs(expr1=operations[0], expr2=operations[1])
        self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)

    async def test_scientific_deadline_exception_remains_an_operator_failure(self):
        original = StartDeadlineExpired("Scientific component raised a deadline error")
        self.science.failure = original

        with self.assertRaises(expected_exception=StartDeadlineExpired) as raised:
            await self.propose(value=1)

        self.assertIs(expr1=original, expr2=raised.exception)

    async def test_interpreter_next_model_prepares_after_original_loading_settles(self):
        self.science.load_entered = Event()
        self.science.load_release = Event()
        first = endpoint("short-window", call("first", "prepare", {"value": 1}))
        second = endpoint("next-model", call("second", "prepare", {"value": 2}))
        first_deadline = []

        def first_model(**settings):
            first_deadline.append(asyncio.get_running_loop().time() + settings["exchange_seconds"])

            return first

        interpreter = Interpreter(
            endpoint_factories=(first_model, lambda **settings: second),
            timeout_seconds=2,
            context_bytes=32768,
            secrets=(),
        )

        self.addAsyncCleanup(interpreter.retire)

        task = asyncio.create_task(coro=interpreter.run(
            messages=[],
            tools=[self.preparation_tool.definition],
            dispatch=self.preparation_tool.dispatch,
        ))

        try:
            self.assertTrue(expr=await asyncio.to_thread(self.science.load_entered.wait, timeout=2))
            expired = self.preparation_tool.operation

            await asyncio.sleep(delay=max(0, first_deadline[0] - asyncio.get_running_loop().time()) + 0.01)
            self.assertFalse(expr=task.done())
            self.science.load_release.set()

            async with asyncio.timeout(5):
                await task

            observed = await expired.observe(after=None, wait_seconds=0)

            self.assertTrue(expr=observed.finished)
            self.assertIsInstance(obj=observed.failures[0].cause, cls=StartDeadlineExpired)
            self.assertEqual(first=1, second=self.science.initializations)
            self.assertEqual(first=[Sample(value=2)], second=self.science.inputs)
            self.assertEqual(first=self.preparation_tool.operation.reference, second=self.preparation_tool.prepared_state_handle.producer)
        finally:
            self.science.load_release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_lost_release_reply_resumes_release_of_the_same_report(self):
        original = OSError("Preparation release reply was lost")
        submit = self.analysis_executor.submit
        releases = []
        reports = []

        def lose_release_reply(*, invocation):
            operation = submit(invocation=invocation)
            release_result = operation.release_result

            def release(*, result):
                reports.append(result)
                control = release_result(result=result)

                releases.append(control)

                if len(releases) == 1:
                    raise original

                return control

            self.enterContext(patch.object(target=operation, attribute="release_result", new=release))

            return operation

        with patch.object(target=self.analysis_executor, attribute="submit", new=lose_release_reply):
            with self.assertRaises(expected_exception=OSError) as raised:
                await self.propose(value=1)

            self.assertIs(expr1=original, expr2=raised.exception)
            await observe_cleanup(control=releases[0])
            await self.preparation_tool.resume()
            await self.preparation_tool.resume()

        self.assertIs(expr1=reports[0], expr2=reports[1])
        self.assertIs(expr1=releases[0], expr2=releases[1])
        self.assertEqual(first=[Sample(value=1)], second=self.science.inputs)

    async def test_foreign_output_is_rejected_before_science(self):
        foreign = Science.kind.output(name="foreign", dispose=dispose)

        with self.assertRaises(expected_exception=ValueError):
            PreparationTool(
                call_binding=self.preparation_tool.tool_definition.call_binding,
                state_output_slot=foreign,
                analysis_executor=self.analysis_executor,
                reservation_ref=self.preparation_tool.reservation_ref,
                operation_policy=self.preparation_tool.operation_policy,
                description="Cannot prepare into an undeclared output",
            )

        self.assertEqual(first=[], second=self.science.inputs)
