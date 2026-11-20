"""Prove process ownership through the installed host, worker and native runner.

Witness files and explicit release barriers distinguish scientific effects from
acknowledgements. They do not replace the installed execution implementation.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
import time
import traceback
import unittest
from unittest.mock import patch

from provider_core.contribution import ref
from provider_core.hosting import (
    AcceptanceState,
    DispatchState,
    Invocation,
    NotAdmitted,
    OperationPolicy,
    Outcome,
    ResourceState,
    ResultState,
    StartDeadlineExpired,
    observe_completion,
)
from provider_core.process_host import ProcessExecutionHost
from tests.fakes.runner import RunningSupervisor
from tests.fakes.scientific_process import Number, WitnessedScience, wait_for_event, witness_events


class ProcessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="science-witness-")))
        self.unrelated = self.enterContext((self.root / "unrelated-handle").open(mode="wb"))

        os.set_inheritable(self.unrelated.fileno(), True)

        self.enterContext(patch.dict(os.environ, {"SCIENTIFIC_PROCESS_UNRELATED": "parent-only"}))

    def make_host(self):
        worker = Path(__file__).parent.parent / "fakes/scientific_process_worker.py"

        runner = RunningSupervisor(
            operation_seconds=None,
            role="science",
            worker_argv=(sys.executable, "-I", "-B", str(worker)),
            environment={
                "PROVIDER_CORE_IN_CONTAINER": os.environ["PROVIDER_CORE_IN_CONTAINER"],
                "SCIENTIFIC_PROCESS_ALLOWED": "explicit-science-grant",
            },
        )

        self.addCleanup(runner.close)
        session = runner.session(startup_seconds=5, retirement_seconds=5)

        host = ProcessExecutionHost(
            session=session,
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=5,
        )

        self.addAsyncCleanup(self.retire_host, host)

        return (host, runner, session)

    async def start(self, host, *, scenario):
        unrelated_identity = os.fstat(self.unrelated.fileno())

        return await host.start(
            contribution_type=WitnessedScience,
            constructor="tests.fakes.scientific_process:construct_science",
            startup={
                "witness_directory": str(self.root),
                "scenario": scenario,
                "unrelated_identity": (unrelated_identity.st_dev, unrelated_identity.st_ino),
            },
            requirements=frozenset(),
        )

    async def cleanup_observation(self, control):
        async with asyncio.timeout(15):
            observed = await control.observe(after=None, wait_seconds=0)

            while (not observed.settled) and (not observed.failures):
                observed = await control.observe(after=observed.revision, wait_seconds=1)

        return observed

    async def retire_host(self, host):
        return await self.cleanup_observation(host.retire())

    async def successful_cleanup(self, control):
        observed = await self.cleanup_observation(control)

        self.assertEqual(first=(), second=observed.failures)
        self.assertTrue(expr=observed.settled)

    def invocation(self, hosted, analysis):
        call = hosted.call(method=WitnessedScience.calculate)

        return Invocation(
            target=ref(call=call),
            payload=call.encode_input(value=Number(value=10)),
            states={},
            reservation=analysis.reservation(allocation=hosted.allocation),
            policy=OperationPolicy(
                queue_stop_request_seconds=5,
                execution_stop_request_seconds=10,
                cleanup_overdue_flag_seconds=5,
                reject_encoded_input_over_bytes=1024,
                reject_encoded_result_over_bytes=1024,
            ),
        )

    def counts(self):
        return Counter(event["event"] for event in witness_events(self.root))

    def assert_native_retirement(self, host, runner, session):
        self.assertIsNotNone(obj=session.retirement)
        self.assertTrue(expr=session.retirement.scratch_released)
        self.assertTrue(expr=host.retired)
        self.assertEqual(first=frozenset(), second=host.allocations)
        self.assertEqual(first=[], second=list(runner.scratch.iterdir()))

    async def test_two_analyses_construct_and_initialize_once_with_explicit_child_grants(self):
        (host, runner, session) = self.make_host()
        hosted = await self.start(host, scenario="reuse")

        for expected_calls in (1, 2):
            analysis = host.open_analysis(allocations=frozenset({hosted.allocation}))
            original = analysis.submit(self.invocation(hosted, analysis))

            self.assertNotIsInstance(obj=original, cls=NotAdmitted)

            observed = await asyncio.wait_for(observe_completion(original), timeout=5)
            calculation = json.loads(await original.read_result(observed.result))

            self.assertIs(expr1=Outcome.SUCCEEDED, expr2=observed.outcome)
            self.assertEqual(first=(), second=observed.failures)
            self.assertEqual(first=expected_calls, second=calculation["calls"])
            self.assertEqual(first=10 + expected_calls, second=calculation["value"])
            self.assertEqual(first=host.process_id, second=calculation["process_id"])
            self.assertNotEqual(first=os.getpid(), second=calculation["process_id"])

            # The second result remains owned through analysis close and must
            # expire when its worker exits.
            if expected_calls == 1:
                await self.successful_cleanup(original.release_result(observed.result))

            await self.successful_cleanup(analysis.close())

        retained_operation = original
        retained = await retained_operation.observe(after=None, wait_seconds=0)

        self.assertIs(expr1=ResultState.AVAILABLE, expr2=retained.result_state)
        acquired = await asyncio.to_thread(wait_for_event, self.root, "initialization_acquired")

        self.assertEqual(first="explicit-science-grant", second=acquired["allowed_environment"])
        self.assertFalse(expr=acquired["unrelated_environment"])
        self.assertFalse(expr=acquired["unrelated_descriptor"])
        self.assertTrue(expr=Path(acquired["sdk_path"]).is_relative_to(Path(acquired["installation"])))

        cleanup = await self.retire_host(host)
        counts = self.counts()
        retired = await retained_operation.observe(after=None, wait_seconds=0)

        self.assertIs(expr1=Outcome.SUCCEEDED, expr2=retired.outcome)
        self.assertIs(expr1=ResultState.RELEASED, expr2=retired.result_state)
        self.assertIs(expr1=ResourceState.RELEASED, expr2=retired.resources)
        self.assertEqual(first=(), second=retired.failures)

        for wait_seconds in (0, 1):
            unchanged = await retained_operation.observe(after=retired.revision, wait_seconds=wait_seconds)

            self.assertEqual(first=retired, second=unchanged)

        self.assertEqual(first=(), second=cleanup.failures)
        self.assertEqual(first=1, second=counts["constructed"])
        self.assertEqual(first=1, second=counts["initialization_acquired"])
        self.assertEqual(first=1, second=counts["initialized"])
        self.assertEqual(first=2, second=counts["effect"])
        self.assertEqual(first=1, second=counts["closed"])
        self.assertEqual(first={host.process_id}, second={event["pid"] for event in witness_events(self.root)})
        self.assertFalse(expr=(self.root / "partial-resource").exists())
        self.assertEqual(first=0, second=session.retirement.returncode)
        self.assert_native_retirement(host, runner, session)

    async def test_cancelled_startup_waiter_keeps_original_load_and_partial_cleanup(self):
        (host, runner, session) = self.make_host()
        waiter = asyncio.create_task(self.start(host, scenario="cancel-start"))
        release = self.root / "release-initialization"

        try:
            await asyncio.to_thread(wait_for_event, self.root, "initialization_acquired")
            original_load = host.start_task

            waiter.cancel()

            with self.assertRaises(expected_exception=asyncio.CancelledError):
                await waiter

            self.assertIs(expr1=original_load, expr2=host.start_task)
            self.assertFalse(expr=original_load.done())
            self.assertTrue(expr=(self.root / "partial-resource").exists())
            self.assertEqual(first=0, second=self.counts()["close_entered"])
            retirement = host.retire()

            self.assertFalse(expr=(await retirement.observe(after=None, wait_seconds=0)).settled)
            release.touch()
            cleanup = await self.cleanup_observation(retirement)

            self.assertEqual(first=(), second=cleanup.failures)
            self.assertFalse(expr=original_load.cancelled())
            self.assertIs(expr1=host.hosted, expr2=original_load.result())
            self.assertEqual(first=1, second=self.counts()["constructed"])
            self.assertEqual(first=1, second=self.counts()["initialized"])
            self.assertEqual(first=1, second=self.counts()["closed"])
            self.assertFalse(expr=(self.root / "partial-resource").exists())
            self.assert_native_retirement(host, runner, session)
        finally:
            release.touch(exist_ok=True)
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_independent_initialization_and_close_failures_retain_both_messages(self):
        (host, runner, session) = self.make_host()

        with self.assertRaises(expected_exception=ExceptionGroup) as raised:
            async with host:
                await self.start(host, scenario="independent-failures")

        report = "".join(traceback.format_exception(raised.exception))

        self.assertIn(member="Witnessed partial model initialization failed", container=report)
        self.assertIn(member="Witnessed partial resource close failed", container=report)
        self.assertEqual(first=1, second=self.counts()["initialization_acquired"])
        self.assertEqual(first=1, second=self.counts()["close_entered"])
        self.assertEqual(first=0, second=self.counts()["closed"])
        self.assertFalse(expr=(self.root / "partial-resource").exists())
        self.assertTrue(expr=(await self.retire_host(host)).failures)
        self.assert_native_retirement(host, runner, session)

    async def test_worker_killed_during_active_science_retains_original_operation_and_retirement(self):
        (host, runner, session) = self.make_host()
        hosted = await self.start(host, scenario="active-kill")

        self.addCleanup((self.root / "release-calculation").touch)
        analysis = host.open_analysis(allocations=frozenset({hosted.allocation}))
        invocation = self.invocation(hosted, analysis)
        original = analysis.submit(invocation)

        await asyncio.to_thread(wait_for_event, self.root, "calculation_started")
        active = await original.observe(after=None, wait_seconds=1)

        self.assertIs(expr1=DispatchState.STARTED, expr2=active.dispatch)
        self.assertFalse(expr=active.finished)
        os.kill(host.process_id, signal.SIGKILL)

        self.assertIs(expr1=original, expr2=analysis.submit(invocation))
        cleanup = await self.retire_host(host)

        self.assertTrue(expr=cleanup.failures)
        self.assertIsNone(obj=host.scientific_retirement)
        self.assertEqual(first=-signal.SIGKILL, second=session.retirement.returncode)
        self.assert_native_retirement(host, runner, session)

        for _ in range(2):
            observed = await original.observe(after=None, wait_seconds=0)

            self.assertEqual(first=original.reference, second=observed.operation)
            self.assertIs(expr1=AcceptanceState.OWNED, expr2=observed.acceptance)
            self.assertIs(expr1=DispatchState.STARTED, expr2=observed.dispatch)
            self.assertTrue(expr=observed.finished)
            self.assertIsNone(obj=observed.outcome)
            self.assertIsNone(obj=observed.result)
            self.assertIs(expr1=ResourceState.RELEASED, expr2=observed.resources)
            self.assertTrue(expr=observed.failures)

        self.assertEqual(first=1, second=self.counts()["calculation_started"])
        self.assertEqual(first=0, second=self.counts()["effect"])
        self.assertEqual(first=0, second=self.counts()["close_entered"])

    async def test_effect_before_lost_reply_is_not_replayed_by_submission_or_observation(self):
        (host, runner, session) = self.make_host()
        hosted = await self.start(host, scenario="lost-reply")
        analysis = host.open_analysis(allocations=frozenset({hosted.allocation}))
        invocation = self.invocation(hosted, analysis)

        def observe_effect_then_release_reply():
            try:
                handoff = wait_for_event(self.root, "reply_waiting")
                effect = (self.root / "effect").read_text(encoding="utf-8")

                return (handoff, effect)
            finally:
                (self.root / "release-reply").touch()

        # submit is synchronous. A separate witness must observe the completed
        # effect while its original acknowledgement remains held in the child.
        with ThreadPoolExecutor(max_workers=1) as witness:
            effect_observation = witness.submit(observe_effect_then_release_reply)
            original = analysis.submit(invocation)
            (handoff, effect) = effect_observation.result(timeout=10)

        self.assertEqual(first=invocation.operation_id, second=handoff["operation_id"])
        self.assertEqual(first="1", second=effect)
        self.assertIs(expr1=original, expr2=analysis.submit(invocation))

        for _ in range(2):
            uncertain = await original.observe(after=None, wait_seconds=0)

            self.assertEqual(first=original.reference, second=uncertain.operation)
            self.assertIs(expr1=AcceptanceState.UNCERTAIN, expr2=uncertain.acceptance)
            self.assertIsNone(obj=uncertain.outcome)
            self.assertIsNone(obj=uncertain.result)
            self.assertIs(expr1=ResultState.NONE, expr2=uncertain.result_state)
            self.assertTrue(expr=uncertain.failures)
            self.assertIsInstance(obj=uncertain.failures[0].cause.__cause__, cls=EOFError)

        cleanup = await self.retire_host(host)
        retired = await original.observe(after=None, wait_seconds=0)

        self.assertTrue(expr=cleanup.failures)
        self.assertEqual(first=original.reference, second=retired.operation)
        self.assertIs(expr1=AcceptanceState.UNCERTAIN, expr2=retired.acceptance)
        self.assertTrue(expr=retired.finished)
        self.assertIsNone(obj=retired.outcome)
        self.assertIs(expr1=ResourceState.RELEASED, expr2=retired.resources)
        self.assertEqual(first=1, second=self.counts()["submit_received"])
        self.assertEqual(first=1, second=self.counts()["calculation_started"])
        self.assertEqual(first=1, second=self.counts()["effect"])
        self.assertEqual(first=1, second=self.counts()["reply_dropped"])
        self.assert_native_retirement(host, runner, session)

    async def test_child_refuses_expired_start_and_accepts_valid_original_identity(self):
        (host, runner, session) = self.make_host()
        hosted = await self.start(host, scenario="expired-start")
        analysis = host.open_analysis(allocations=frozenset({hosted.allocation}))
        invocation = self.invocation(hosted, analysis)
        expired = replace(invocation, start_not_after=time.monotonic() - 1)

        with self.assertRaises(expected_exception=StartDeadlineExpired):
            analysis.submit(expired)

        (received,) = [event for event in witness_events(self.root) if event["event"] == "submit_received"]

        self.assertEqual(first=expired.operation_id, second=received["operation_id"])
        self.assertEqual(first=0, second=self.counts()["calculation_started"])
        self.assertIsNone(obj=host.channel.failure)

        original = analysis.submit(invocation)
        observed = await asyncio.wait_for(observe_completion(original), timeout=5)
        calculation = json.loads(await original.read_result(observed.result))

        self.assertIs(expr1=Outcome.SUCCEEDED, expr2=observed.outcome)
        self.assertEqual(first=invocation.operation_id, second=original.reference.operation_id)
        self.assertEqual(first=1, second=calculation["calls"])
        self.assertEqual(first=2, second=self.counts()["submit_received"])
        self.assertEqual(first=1, second=self.counts()["effect"])

        await self.successful_cleanup(original.release_result(observed.result))
        await self.successful_cleanup(analysis.close())

        cleanup = await self.retire_host(host)

        self.assertEqual(first=(), second=cleanup.failures)
        self.assertEqual(first=0, second=session.retirement.returncode)
        self.assert_native_retirement(host, runner, session)


if __name__ == "__main__":
    unittest.main()
