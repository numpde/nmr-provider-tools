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
import fcntl
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
from provider_core.execution_setup import ExecutionSettings
from provider_core.hosting import (
    AcceptanceState,
    AdmissionCode,
    CapacityUnavailable,
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
from provider_core.scientific_call import ErrorPolicy, ScientificCall
from provider_core.runner import LOCK_NAME
from provider_core.runner_session import RunnerStartupUnavailable
from tests.fakes.runner import RunningSupervisor
from tests.fakes.scientific_process import Number, WitnessedScience, construct_science, wait_for_event, witness_events


class ProcessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="science-witness-")))
        self.unrelated = self.enterContext((self.root / "unrelated-handle").open(mode="wb"))

        os.set_inheritable(self.unrelated.fileno(), True)

        self.enterContext(patch.dict(os.environ, {"SCIENTIFIC_PROCESS_UNRELATED": "parent-only"}))

    def make_runner(self):
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

        return runner

    # silent-defaults: allow-contract -- Ordinary proofs use five seconds; specific lifetime witnesses shorten the scientific or native allowance
    def make_host(self, *, scientific_retirement_seconds=5, native_retirement_seconds=5, startup_not_after=None):
        runner = self.make_runner()
        session = runner.session(startup_seconds=5, retirement_seconds=native_retirement_seconds, startup_not_after=startup_not_after)

        host = ProcessExecutionHost(
            session=session,
            input_bytes=4096,
            result_bytes=4096,
            state_values=1,
            progress_records=1,
            initialization_seconds=5,
            retirement_seconds=scientific_retirement_seconds,
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

    def managed_call(self, *, host, hosted):
        return ScientificCall(
            host=host,
            allocation=hosted.allocation,
            binding=hosted.call(method=WitnessedScience.calculate),
            operation_policy=OperationPolicy(
                queue_stop_request_seconds=5,
                execution_stop_request_seconds=10,
                cleanup_overdue_flag_seconds=5,
                reject_encoded_input_over_bytes=1024,
                reject_encoded_result_over_bytes=1024,
            ),
            error_policy=ErrorPolicy(max_attempts=3),
        )

    async def test_managed_calls_release_scopes_and_reuse_the_loaded_model(self):
        (host, _, _) = self.make_host()
        hosted = await self.start(host, scenario="healthy")
        calculate = self.managed_call(host=host, hosted=hosted)

        for expected_calls in range(1, 4):
            prediction = await calculate(scientific_input=Number(value=10))

            self.assertEqual(first=10 + expected_calls, second=prediction.value)
            self.assertEqual(first=expected_calls, second=prediction.calls)
            self.assertEqual(first=host.process_id, second=prediction.process_id)
            self.assertIsNone(obj=host.active_analysis)

        self.assertEqual(first=1, second=self.counts()["initialized"])

    async def test_managed_cancellation_finishes_cleanup_before_the_next_call(self):
        (host, _, _) = self.make_host()
        hosted = await self.start(host, scenario="active-kill")
        calculate = self.managed_call(host=host, hosted=hosted)
        invocation_task = asyncio.create_task(coro=calculate(scientific_input=Number(value=10)))

        try:
            await asyncio.to_thread(wait_for_event, self.root, "calculation_started")
            invocation_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(expr=invocation_task.done())
        finally:
            (self.root / "release-calculation").touch()

        with self.assertRaises(expected_exception=asyncio.CancelledError):
            await invocation_task

        self.assertIsNone(obj=host.active_analysis)
        prediction = await calculate(scientific_input=Number(value=20))

        self.assertEqual(first=22, second=prediction.value)
        self.assertEqual(first=2, second=self.counts()["calculation_started"])

    def configured_call(self, *, runner, scenario, error_policy=None):
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
                    "execution_stop_request_seconds": 5,
                    "cleanup_overdue_flag_seconds": 5,
                    "reject_encoded_input_over_bytes": 1024,
                    "reject_encoded_result_over_bytes": 1024,
                },
                "resource_requirement": [],
                "error_policy": {} if error_policy is None else error_policy,
            },
        )

        unrelated = os.fstat(self.unrelated.fileno())

        return settings.open_process_call(
            runner={
                "socket": str(runner.socket_path),
                "lease_file": str(runner.endpoint / LOCK_NAME),
                "role": "science",
                "startup_seconds": 5,
                "retirement_seconds": 5,
            },
            contribution_type=WitnessedScience,
            constructor=construct_science,
            startup={
                "witness_directory": str(self.root),
                "scenario": scenario,
                "unrelated_identity": (unrelated.st_dev, unrelated.st_ino),
            },
            method=WitnessedScience.calculate,
            identity_method=WitnessedScience.runtime,
        )

    async def test_configured_call_owns_startup_and_retirement_after_caller_failure(self):
        runner = self.make_runner()
        caller_failure = OSError("Provider request handling failed")

        with self.assertRaises(expected_exception=OSError) as raised:
            async with self.configured_call(runner=runner, scenario="healthy") as calculate:
                prediction = await calculate(scientific_input=Number(value=10))

                self.assertEqual(first=11, second=prediction.value)

                raise caller_failure

        self.assertIs(expr1=caller_failure, expr2=raised.exception)
        self.assert_native_retirement(calculate.host, runner, calculate.host.session)
        self.assertEqual(first=1, second=self.counts()["closed"])

    async def test_configured_call_cleans_up_partial_model_initialization(self):
        runner = self.make_runner()

        with self.assertRaises(expected_exception=ExceptionGroup) as raised:
            async with self.configured_call(runner=runner, scenario="independent-failures"):
                self.fail(msg="Partial model initialization must not yield a callable")

        report = "".join(traceback.format_exception(raised.exception))

        self.assertIn(member="Witnessed partial model initialization failed", container=report)
        self.assertIn(member="Witnessed partial resource close failed", container=report)
        self.assertEqual(first=[], second=list(runner.scratch.iterdir()))
        self.assertFalse(expr=(self.root / "partial-resource").exists())

    async def test_explicit_owner_closes_startup_before_a_callable_can_escape(self):
        runner = self.make_runner()
        calculate = self.configured_call(runner=runner, scenario="cancel-start")

        self.assertIsNone(obj=calculate.host)

        starting = asyncio.create_task(coro=calculate.start())

        try:
            await asyncio.to_thread(wait_for_event, self.root, "initialization_acquired")

            closing = asyncio.create_task(coro=calculate.close())

            await asyncio.sleep(0.05)

            self.assertFalse(expr=closing.done())
        finally:
            (self.root / "release-initialization").touch()

        with self.assertRaises(expected_exception=RuntimeError):
            await starting

        await closing
        await calculate.close()

        self.assert_native_retirement(calculate.host, runner, calculate.host.session)
        self.assertEqual(first=0, second=self.counts()["calculation_started"])

    async def test_repeated_close_does_not_forget_an_unconfirmed_scientific_retirement(self):
        runner = self.make_runner()
        calculate = self.configured_call(runner=runner, scenario="independent-failures")

        with self.assertRaises(expected_exception=OSError):
            await calculate.start()

        for _ in range(2):
            with self.assertRaises(expected_exception=BaseException) as raised:
                await calculate.close()

            report = "".join(traceback.format_exception(raised.exception))

            self.assertIn(member="Witnessed partial resource close failed", container=report)

        self.assertEqual(first=1, second=self.counts()["close_entered"])
        self.assertEqual(first=[], second=list(runner.scratch.iterdir()))

    async def test_configured_retry_retires_before_replacement_and_reuses_exact_input(self):
        runner = self.make_runner()

        async with self.configured_call(
            runner=runner,
            scenario="retry-once",
            error_policy={"max_attempts": 2, "retry_scientific_failures": True, "total_seconds": 5},
        ) as calculate:
            original = calculate.host
            identity = calculate.runtime_identity
            number = Number(value=10)

            async def mutate_caller_input(seconds):
                object.__setattr__(number, "value", 99)

            with patch(target="provider_core.scientific_call.sleep", side_effect=mutate_caller_input):
                prediction = await calculate(scientific_input=number)

            self.assertEqual(first=11, second=prediction.value)
            self.assertNotEqual(first=original.process_id, second=calculate.host.process_id)
            self.assertTrue(expr=original.retired)
            self.assertTrue(expr=original.session.retirement.scratch_released)
            self.assertFalse(expr=(runner.scratch / original.session.operation).exists())
            self.assertEqual(first=identity, second=calculate.runtime_identity)

        self.assertEqual(first=[], second=list(runner.scratch.iterdir()))

        events = witness_events(self.root)
        calculations = [event for event in events if event["event"] == "calculation_started"]
        self.assertEqual(first=[10, 10], second=[event["value"] for event in calculations])
        names = [event["event"] for event in events]
        self.assertLess(a=names.index("closed"), b=names.index("constructed", names.index("constructed") + 1))
        self.assertEqual(first=2, second=self.counts()["closed"])

    async def test_changed_replacement_runtime_preserves_first_failure_without_redispatch(self):
        runner = self.make_runner()

        with self.assertRaises(expected_exception=ValueError) as raised:
            async with self.configured_call(
                runner=runner,
                scenario="retry-changed-runtime",
                error_policy={"max_attempts": 2, "retry_scientific_failures": True},
            ) as calculate:
                await calculate(scientific_input=Number(value=10))

        self.assertIn(member="Replacement scientific runtime", container=str(raised.exception))
        self.assertIn(member="Witnessed retryable calculation failure", container=str(raised.exception.__cause__))
        self.assertEqual(first=1, second=self.counts()["calculation_started"])
        self.assertEqual(first=2, second=self.counts()["closed"])
        self.assertEqual(first=[], second=list(runner.scratch.iterdir()))

    async def test_concurrent_call_waits_until_replacement_has_finished(self):
        runner = self.make_runner()

        async with self.configured_call(
            runner=runner,
            scenario="retry-retirement-gate",
            error_policy={"max_attempts": 2, "retry_scientific_failures": True, "total_seconds": 5},
        ) as calculate:
            first = asyncio.create_task(coro=calculate(scientific_input=Number(value=10)))

            try:
                await asyncio.to_thread(wait_for_event, self.root, "close_entered")

                second = asyncio.create_task(coro=calculate(scientific_input=Number(value=20)))

                await asyncio.sleep(0.05)

                self.assertFalse(expr=second.done())
                self.assertEqual(first=1, second=self.counts()["calculation_started"])
            finally:
                (self.root / "release-retirement").touch()

            results = await asyncio.gather(first, second)

            self.assertEqual(first=[11, 22], second=[result.value for result in results])
            self.assertEqual(first=results[0].process_id, second=results[1].process_id)

    async def test_cancelled_recovery_joins_retirement_and_never_acquires_a_replacement(self):
        runner = self.make_runner()

        async with self.configured_call(
            runner=runner,
            scenario="retry-retirement-gate",
            error_policy={"max_attempts": 2, "retry_scientific_failures": True},
        ) as calculate:
            invocation = asyncio.create_task(coro=calculate(scientific_input=Number(value=10)))

            try:
                await asyncio.to_thread(wait_for_event, self.root, "close_entered")
                invocation.cancel()

                await asyncio.sleep(0.05)

                self.assertFalse(expr=invocation.done())
            finally:
                (self.root / "release-retirement").touch()

            with self.assertRaises(expected_exception=asyncio.CancelledError) as raised:
                await invocation

            report = "".join(traceback.format_exception(raised.exception))

            self.assertIn(member="Witnessed retryable calculation failure", container=report)
            self.assertEqual(first=1, second=self.counts()["constructed"])
            self.assert_native_retirement(calculate.host, runner, calculate.host.session)

    async def test_deferred_start_cannot_renew_the_native_setup_cap(self):
        (host, _, session) = self.make_host(startup_not_after=time.monotonic() - 1)

        with self.assertRaises(RunnerStartupUnavailable):
            await self.start(host, scenario="healthy")

        self.assertFalse(session.control_connected)
        self.assertIsNone(session.operation)
        self.assertEqual(0, self.counts()["initialization_acquired"])

    async def test_native_setup_cap_does_not_expire_scientific_initialization(self):
        cap = time.monotonic() + 2
        (host, _, _) = self.make_host(startup_not_after=cap)
        original = asyncio.create_task(self.start(host, scenario="cancel-start"))

        try:
            await asyncio.to_thread(wait_for_event, self.root, "initialization_acquired")
            await asyncio.sleep(max(0, cap - time.monotonic()))

            self.assertFalse(original.done())
        finally:
            (self.root / "release-initialization").touch()
            await original

        self.assertEqual(1, self.counts()["initialized"])

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

    async def test_unrelated_exec_cannot_retain_the_scientific_workers_lifetime_lease(self):
        (host, runner, session) = self.make_host()
        probe_started = False
        try:
            await self.start(host, scenario="exec-lease")
            probe = await asyncio.to_thread(wait_for_event, self.root, "exec_probe_started")
            probe_started = True
            self.assertFalse(expr=probe["lease_present"])
            await self.successful_cleanup(host.retire())
            self.assert_native_retirement(host, runner, session)

            # The supervisor also owns the lease while accepting sessions.
            # Reap it without waiting for the probe's inherited stderr pipe.
            runner.supervisor_process.terminate()
            await asyncio.to_thread(runner.supervisor_process.wait, timeout=3)

            (self.root / "worker-retired").touch()
            await asyncio.to_thread(wait_for_event, self.root, "exec_survived_retirement")
            descriptor = os.open(runner.endpoint / LOCK_NAME, os.O_RDWR | os.O_CLOEXEC)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        finally:
            (self.root / "release-exec-probe").touch()
            if probe_started:
                await asyncio.to_thread(wait_for_event, self.root, "exec_probe_done")

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

    async def test_idle_observers_do_not_spend_retirement_allowance_or_prevent_science(self):
        native_retirement_seconds = 0.5
        (host, runner, session) = self.make_host(native_retirement_seconds=native_retirement_seconds)
        hosted = await self.start(host, scenario="healthy")
        observer = asyncio.create_task(host.observe_exit(wait_seconds=5))

        await asyncio.sleep(0)
        observer.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await observer

        with self.assertRaises(TimeoutError):
            await host.observe_exit(wait_seconds=native_retirement_seconds + 0.1)

        self.assertIsNone(host.native.retirement_deadline)
        self.assertIsNone(session.retirement)
        self.assertIsNone(session.original_exit)
        self.assertFalse(host.retiring)

        analysis = host.open_analysis(allocations=frozenset({hosted.allocation}))
        operation = analysis.submit(self.invocation(hosted, analysis))
        observed = await observe_completion(operation)

        self.assertIs(Outcome.SUCCEEDED, observed.outcome)
        self.assertEqual(host.process_id, json.loads(await operation.read_result(observed.result))["process_id"])
        await self.successful_cleanup(operation.release_result(observed.result))
        await self.successful_cleanup(analysis.close())
        await self.successful_cleanup(host.retire())
        self.assertIsNone(await host.observe_exit(wait_seconds=1))
        self.assert_native_retirement(host, runner, session)
        self.assertEqual(-1, session.control.fileno())

        for thread in host.native._executor._threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())

    async def test_idle_worker_exit_is_shared_and_retained_before_host_retirement(self):
        (host, runner, session) = self.make_host()
        hosted = await self.start(host, scenario="healthy")
        observers = [asyncio.create_task(host.observe_exit(wait_seconds=2)) for _ in range(2)]

        os.kill(host.process_id, signal.SIGKILL)
        first, second = await asyncio.gather(*observers)

        self.assertIs(first, second)
        self.assertIs(first, session.original_exit)
        self.assertEqual(session.role, first.role)
        self.assertEqual(session.generation, first.generation)
        self.assertEqual(session.operation, first.operation)
        self.assertEqual(-signal.SIGKILL, first.returncode)
        self.assertIsNone(host.native.retirement_deadline)
        self.assertIsNone(session.retirement)
        self.assertFalse(host.retiring)

        with self.assertRaises(CapacityUnavailable):
            host.open_analysis(allocations=frozenset({hosted.allocation}))

        self.assertIsNone(host.active_analysis)
        self.assertIsNone(host.channel.failure)
        cleanup = await self.retire_host(host)

        self.assertTrue(cleanup.settled)
        self.assertIs(first, await host.observe_exit(wait_seconds=1))
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

        original_exit = await host.observe_exit(wait_seconds=2)

        self.assertIs(original_exit, session.original_exit)
        self.assertEqual(-signal.SIGKILL, original_exit.returncode)
        self.assertIsNone(host.native.retirement_deadline)
        before_cleanup = await original.observe(after=active.revision, wait_seconds=1)

        self.assertTrue(before_cleanup.finished)
        self.assertIsNone(before_cleanup.outcome)
        self.assertIs(ResourceState.UNKNOWN, before_cleanup.resources)
        self.assertIsNone(host.channel.failure)

        self.assertIs(expr1=original, expr2=analysis.submit(invocation))
        refused = analysis.submit(replace(invocation, operation_id="new-work-after-worker-exit"))

        self.assertIsInstance(refused, NotAdmitted)
        self.assertIs(AdmissionCode.CLOSED, refused.code)
        self.assertIsNone(host.channel.failure)
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

    async def test_retirement_expires_original_science_grace_before_native_stop(self):
        (host, runner, session) = self.make_host(scientific_retirement_seconds=0.2)
        hosted = await self.start(host, scenario="active-kill")

        self.addCleanup((self.root / "release-calculation").touch)
        analysis = host.open_analysis(allocations=frozenset({hosted.allocation}))
        invocation = self.invocation(hosted, analysis)
        original = analysis.submit(invocation)

        await asyncio.to_thread(wait_for_event, self.root, "calculation_started")
        active = await original.observe(after=None, wait_seconds=1)

        self.assertIs(expr1=DispatchState.STARTED, expr2=active.dispatch)
        self.assertFalse(expr=active.finished)
        retirement = host.retire()

        self.assertIs(expr1=retirement, expr2=host.retire())
        cleanup = await self.cleanup_observation(retirement)

        self.assertTrue(expr=cleanup.settled)
        self.assertTrue(expr=cleanup.allowance_expired)
        self.assertTrue(
            expr=any(
                ((failure.phase == "cleanup") and isinstance(failure.cause, TimeoutError))
                for failure in cleanup.failures
            ),
        )

        self.assertIsNone(obj=host.scientific_retirement)
        self.assertEqual(first="owner_lost", second=session.retirement.reason)
        self.assertIn(member=session.retirement.returncode, container={-signal.SIGTERM, -signal.SIGKILL})
        self.assertIsNone(obj=session.original_exit)
        self.assert_native_retirement(host, runner, session)
        retired = await original.observe(after=None, wait_seconds=0)

        self.assertEqual(first=original.reference, second=retired.operation)
        self.assertIs(expr1=AcceptanceState.OWNED, expr2=retired.acceptance)
        self.assertIs(expr1=DispatchState.STARTED, expr2=retired.dispatch)
        self.assertTrue(expr=retired.finished)
        self.assertIsNone(obj=retired.outcome)
        self.assertIsNone(obj=retired.result)
        self.assertIs(expr1=ResultState.NONE, expr2=retired.result_state)
        self.assertIs(expr1=ResourceState.RELEASED, expr2=retired.resources)
        self.assertTrue(expr=retired.failures)
        native_deadline = host.native.retirement_deadline
        native_reader = host.native.reader
        native_receipt = session.retirement

        self.assertIsNotNone(obj=native_deadline)
        self.assertIsNotNone(obj=native_reader)

        for wait_seconds in (0, 1):
            self.assertIs(expr1=retirement, expr2=host.retire())
            unchanged_cleanup = await retirement.observe(after=None, wait_seconds=wait_seconds)
            unchanged_operation = await original.observe(after=retired.revision, wait_seconds=wait_seconds)

            self.assertEqual(first=cleanup, second=unchanged_cleanup)
            self.assertEqual(first=retired, second=unchanged_operation)
            self.assertEqual(first=native_deadline, second=host.native.retirement_deadline)
            self.assertIs(expr1=native_reader, expr2=host.native.reader)
            self.assertIs(expr1=native_receipt, expr2=session.retirement)

        self.assertFalse(expr=(self.root / "release-calculation").exists())
        self.assertEqual(first=1, second=self.counts()["constructed"])
        self.assertEqual(first=1, second=self.counts()["submit_received"])
        self.assertEqual(first=1, second=self.counts()["calculation_started"])
        self.assertEqual(first=0, second=self.counts()["effect"])
        self.assertEqual(first=0, second=self.counts()["close_entered"])

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
