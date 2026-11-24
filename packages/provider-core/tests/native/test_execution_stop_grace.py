"""An execution deadline retires uncooperative owned workers without replay."""

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from provider_core.batch_call import BatchCall, BatchPolicy
from provider_core.contribution import InterruptionSupport, ScientificControl
from provider_core.execution_setup import ExecutionSettings
from provider_core.hosting import CleanupObservation, Failure
from provider_core.payload import JsonDataclassCodec
from provider_core.scientific import Contribution, operation
from tests.fakes.runner import RunningSupervisor


@dataclass
class Request:
    cooperative: bool


class DeadlineScience(Contribution):
    def __init__(self, *, startup):
        super().__init__()
        self.witness = Path(startup["witness_directory"])

    @operation(interruption=InterruptionSupport.COOPERATIVE, retryable_failures=(TimeoutError,))
    def calculate(self, *, request: Request, control: ScientificControl) -> Request:
        with (self.witness / "calls").open("a") as calls:
            calls.write(f"{os.getpid()}\n")

        while not control.stop_requested:
            time.sleep(0.005)

        (self.witness / "stopped").touch()

        while not request.cooperative:
            time.sleep(0.01)

        return request


def construct(*, startup):
    return DeadlineScience(startup=startup)


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for member in error.exceptions for leaf in leaves(member)]

    return [error]


class ExecutionStopGraceTests(unittest.IsolatedAsyncioTestCase):
    async def open_call(self, *, grace=0.25, execution_seconds=0.25, attempts=2, scenario="healthy"):
        witness = Path(self.enterContext(TemporaryDirectory()))

        runner = RunningSupervisor(
            operation_seconds=None,
            role="science",
            worker_argv=(sys.executable, "-I", "-B", str(Path(__file__).parents[1] / "fakes/scientific_process_worker.py")),
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
                    "retirement_seconds": 0.15,
                },
                "operation_policy": {
                    "queue_stop_request_seconds": 5,
                    "execution_stop_request_seconds": execution_seconds,
                    "execution_stop_grace_seconds": grace,
                    "cleanup_overdue_flag_seconds": 1,
                    "reject_encoded_input_over_bytes": 4096,
                    "reject_encoded_result_over_bytes": 4096,
                },
                "error_policy": {"max_attempts": attempts, "retry_scientific_failures": True},
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
            contribution_type=DeadlineScience,
            constructor=construct,
            startup={"witness_directory": str(witness), "scenario": scenario},
            method=DeadlineScience.calculate,
        )

        self.addAsyncCleanup(self.close_call, call)
        await call.start()

        return (call, witness)

    async def close_call(self, call):
        try:
            await call.close()
        except Exception:
            # Tests assert retirement diagnostics before teardown; runner.close
            # independently joins any child left by an injected cleanup failure.
            pass

    async def wait_file(self, path):
        async with asyncio.timeout(3):
            while not path.exists():
                await asyncio.sleep(0.01)

    async def completed_failure(self, invocation):
        (completed, _) = await asyncio.wait({invocation}, timeout=4)

        self.assertIn(invocation, completed, "Execution grace must bound an uncooperative worker")

        with self.assertRaises(BaseException) as raised:
            await invocation

        return leaves(raised.exception)

    async def test_uncooperative_worker_is_retired_without_retry(self):
        (call, witness) = await self.open_call()
        invocation = asyncio.create_task(call(scientific_input=Request(cooperative=False)))

        await self.wait_file(witness / "stopped")
        await asyncio.sleep(0.1)
        self.assertFalse(call.closed)
        failures = await self.completed_failure(invocation)

        self.assertTrue(any(isinstance(error, TimeoutError) and "execution_stop_grace_seconds" in str(error) for error in failures))
        self.assertTrue(call.closed)
        self.assertTrue(call.host.retired)
        self.assertTrue(call.host.session.retirement.scratch_released)
        self.assertIsNone(call.host.active_analysis)
        self.assertEqual(1, len((witness / "calls").read_text().splitlines()))

    async def test_cooperative_completion_during_grace_keeps_worker_reusable(self):
        (call, witness) = await self.open_call(grace=1)

        self.assertEqual(Request(cooperative=True), await call(scientific_input=Request(cooperative=True)))
        self.assertFalse(call.closed)
        await asyncio.sleep(1.1)
        self.assertEqual(Request(cooperative=True), await call(scientific_input=Request(cooperative=True)))
        self.assertEqual(1, len(set((witness / "calls").read_text().splitlines())))

    async def test_cancellation_cannot_bypass_escalation_and_cleanup(self):
        (call, witness) = await self.open_call(execution_seconds=30)
        invocation = asyncio.create_task(call(scientific_input=Request(cooperative=False)))

        await self.wait_file(witness / "calls")
        invocation.cancel()
        failures = await self.completed_failure(invocation)

        self.assertTrue(any(isinstance(error, asyncio.CancelledError) for error in failures))
        self.assertTrue(call.host.retired)
        self.assertEqual(1, len((witness / "calls").read_text().splitlines()))

    async def test_failed_retirement_retains_reservation_and_independent_failure(self):
        (call, witness) = await self.open_call()
        cleanup_error = OSError("Injected worker retirement failure")

        async def failed_retirement():
            return CleanupObservation(0, False, False, (Failure("cleanup", cleanup_error),))

        with patch.object(call.host, "_shutdown", side_effect=failed_retirement):
            invocation = asyncio.create_task(call(scientific_input=Request(cooperative=False)))
            failures = await self.completed_failure(invocation)

        self.assertIn(cleanup_error, failures)
        self.assertTrue(any(isinstance(error, TimeoutError) for error in failures))
        self.assertFalse(call.host.retired)
        self.assertIsNotNone(call.host.active_analysis)
        self.assertTrue(call.closed)
        self.assertEqual(1, len((witness / "calls").read_text().splitlines()))

    async def test_borrowed_call_cannot_enable_worker_retirement(self):
        (call, witness) = await self.open_call()

        with self.assertRaisesRegex(ValueError, "owning ProcessCall"):
            call.settings.bind_call(host=call.host, contribution=call.host.hosted, method=DeadlineScience.calculate)

        self.assertFalse((witness / "calls").exists())

    async def check_batch_retirement(self, *, confirmed):
        (call, witness) = await self.open_call(attempts=1)

        batches = BatchCall(
            call=call,
            policy=BatchPolicy(maximum_entries=1, collection_seconds=0.01, maximum_queued_input_bytes=4096, maximum_entry_input_bytes=1024),
            entry_codec=JsonDataclassCodec(format="test.grace.request.v1", value_type=Request),
            pack=lambda requests: requests[0],
            unpack=lambda result: (result,),
        )

        await batches.__aenter__()
        self.addAsyncCleanup(self.close_call, batches)
        entered_retirement = asyncio.Event()
        release_retirement = asyncio.Event()

        self.addCleanup(release_retirement.set)
        original_shutdown = call.host._shutdown
        cleanup_error = OSError("Injected shared-worker retirement failure")

        async def gated_retirement():
            entered_retirement.set()
            await release_retirement.wait()

            if confirmed:
                return await original_shutdown()

            return CleanupObservation(0, False, False, (Failure("cleanup", cleanup_error),))

        with patch.object(call.host, "_shutdown", side_effect=gated_retirement):
            entries = [await batches.prepare(Request(cooperative=False)) for _ in range(2)]
            invocations = [asyncio.create_task(entry.execute(require_execution=lambda: None)) for entry in entries]
            charged_bytes = batches.input_bytes

            async with asyncio.timeout(3):
                await entered_retirement.wait()

            self.assertGreater(charged_bytes, 0)
            self.assertEqual(charged_bytes, batches.input_bytes)
            self.assertTrue(all(not entry.settled.done() for entry in entries))
            self.assertIsNotNone(call.host.active_analysis)
            release_retirement.set()

            for invocation in invocations:
                await self.completed_failure(invocation)

        self.assertEqual(1, len((witness / "calls").read_text().splitlines()))
        self.assertTrue(all(entry.entry_result is None for entry in entries))

        if confirmed:
            self.assertEqual(0, batches.input_bytes)

            for entry in entries:
                await entry.retire()
        else:
            self.assertEqual(charged_bytes, batches.input_bytes)

            for entry in entries:
                with self.assertRaises(Exception) as raised:
                    await entry.retire()

                self.assertIn(cleanup_error, leaves(raised.exception))

    async def test_batch_releases_participants_only_after_confirmed_retirement(self):
        await self.check_batch_retirement(confirmed=True)

    async def test_batch_retains_participants_when_retirement_fails(self):
        await self.check_batch_retirement(confirmed=False)

    async def test_lost_observation_retires_original_worker_without_replay(self):
        (call, witness) = await self.open_call(scenario="lost-reply")
        invocation = asyncio.create_task(call(scientific_input=Request(cooperative=False)))

        await self.completed_failure(invocation)

        self.assertTrue(call.closed)
        self.assertTrue(call.host.retired)
        self.assertEqual(1, len((witness / "calls").read_text().splitlines()))
