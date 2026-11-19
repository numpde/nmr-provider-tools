"""Exercise the scientific worker's analysis and retained-result boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import socket
import unittest
from unittest.mock import patch

from provider_core.contribution import ref
from provider_core.hosting import (
    AdmissionCode,
    CapacityUnavailable,
    CleanupObservation,
    Invocation,
    OperationPolicy,
    ReservationRef,
    ResourceState,
    ResultState,
)
from provider_core.process_wire import RemoteScienceError, receive_async, send_async
from provider_core.process_worker import _Worker, _installed_member, _serve
from provider_core.scientific import (
    AssetAccess,
    Contribution,
    HostedContribution,
    ResourceRequirement,
    operation,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Number:
    value: int


class Counter(Contribution):
    def __init__(self, *, fail_initialization: bool):
        super().__init__()
        self.fail_initialization = fail_initialization
        self.calls = 0

    def initialize(self, *, assets: AssetAccess) -> None:
        if self.fail_initialization:
            raise OSError("Scientific initialization failed")

    @operation()
    def calculate(self, *, number: Number) -> Number:
        self.calls += 1
        return Number(value=number.value + self.calls)


def construct_counter(*, startup):
    return Counter(fail_initialization=startup["fail_initialization"])


class ProcessWorkerTests(unittest.TestCase):
    def test_installed_contribution_class_is_resolved_by_module_and_name(self):
        self.assertIs(Contribution, _installed_member("provider_core.scientific:Contribution"))

    def test_nonportable_local_construction_is_refused(self):
        with self.assertRaises(ValueError):
            _installed_member("my_module:outer.<locals>.factory")


class ProcessWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = _Worker()
        self.addAsyncCleanup(self.worker.retire)

    async def start_worker(self, *, fail_initialization):
        requirements = frozenset({ResourceRequirement(kind="cpu", amount=1, unit="thread")})
        ready = await self.worker.command({
            "action": "start",
            "host_options": {
                "input_bytes": 2048,
                "result_bytes": 2048,
                "state_values": 1,
                "progress_records": 1,
                "initialization_seconds": 2,
                "retirement_seconds": 2,
            },
            "constructor": f"{__name__}:construct_counter",
            "contribution_type": f"{__name__}:Counter",
            "startup": {"fail_initialization": fail_initialization},
            "requirements": tuple(requirements),
        })
        self.assertEqual(first=os.getpid(), second=ready["process_id"])
        allocation = ready["allocation"]
        self.hosted = HostedContribution(
            contribution=None,
            contribution_type=Counter,
            host=allocation.host,
            allocation_name=allocation.allocation,
            requirements=requirements,
        )

    def invocation(self, *, reservation):
        call = self.hosted.call(method=Counter.calculate)
        return Invocation(
            target=ref(call=call),
            payload=call.encode_input(value=Number(value=4)),
            states={},
            reservation=reservation,
            policy=OperationPolicy(
                queue_stop_request_seconds=2,
                execution_stop_request_seconds=2,
                cleanup_overdue_flag_seconds=2,
                reject_encoded_input_over_bytes=1024,
                reject_encoded_result_over_bytes=1024,
            ),
        )

    async def completed(self, *, operation):
        async with asyncio.timeout(5):
            while True:
                observed = await self.worker.command({
                    "action": "observe",
                    "analysis": operation.analysis,
                    "operation": operation.operation_id,
                })
                if observed.finished:
                    return observed
                await asyncio.sleep(0)

    async def cleaned(self, message):
        async with asyncio.timeout(5):
            while True:
                reply = await self.worker.command(message)
                observed = reply if isinstance(reply, CleanupObservation) else reply["cleanup"]
                self.assertFalse(expr=observed.failures)
                if observed.settled:
                    return reply
                await asyncio.sleep(0)

    async def test_failed_factory_resolution_retains_host_for_cleanup(self):
        options = {
            "input_bytes": 2048,
            "result_bytes": 2048,
            "state_values": 1,
            "progress_records": 1,
            "initialization_seconds": 2,
            "retirement_seconds": 2,
        }

        with self.assertRaises(expected_exception=ModuleNotFoundError):
            await self.worker.command({
                "action": "start",
                "host_options": options,
                "constructor": "not_installed_scientific_factory:create",
                "contribution_type": "provider_core.scientific:Contribution",
                "startup": {},
                "requirements": (),
            })

        observation = await self.worker.command({"action": "retire"})
        self.assertTrue(observation.settled)
        self.assertEqual((), observation.failures)

    async def test_failed_initialization_does_not_publish_readiness(self):
        with self.assertRaises(expected_exception=OSError):
            await self.start_worker(fail_initialization=True)

        with self.assertRaises(expected_exception=RuntimeError):
            await self.worker.command({"action": "open_analysis", "analysis": "unready"})

        observed = await self.worker.command({"action": "retire"})
        self.assertTrue(expr=observed.settled)

    async def test_submission_cannot_create_or_reopen_an_analysis(self):
        await self.start_worker(fail_initialization=False)
        allocation = self.hosted.allocation
        reservation = ReservationRef(
            host=allocation.host,
            analysis="original",
            allocation=allocation.allocation,
            binding_id=allocation.binding_id,
            binding_revision=allocation.binding_revision,
        )
        invocation = self.invocation(reservation=reservation)
        command = {"action": "submit", "invocation": invocation}
        refused = await self.worker.command(command)
        self.assertIs(expr1=AdmissionCode.INVALID_REFERENCE, expr2=refused["refusal"].code)

        opened = await self.worker.command({"action": "open_analysis", "analysis": "original"})
        self.assertEqual(first=reservation, second=opened)
        admitted = await self.worker.command(command)
        observed = await self.completed(operation=admitted["operation"])
        payload = await self.worker.command({
            "action": "read_result",
            "analysis": "original",
            "operation": invocation.operation_id,
            "result": observed.result,
        })
        self.assertEqual(first={"value": 5}, second=json.loads(payload))

        await self.cleaned({
            "action": "release_result",
            "analysis": "original",
            "operation": invocation.operation_id,
            "result": observed.result,
        })
        await self.cleaned({"action": "close_analysis", "analysis": "original"})
        final = await self.worker.command({
            "action": "observe",
            "analysis": "original",
            "operation": invocation.operation_id,
        })
        self.assertIs(expr1=ResultState.RELEASED, expr2=final.result_state)
        self.assertIs(expr1=ResourceState.RELEASED, expr2=final.resources)

        await self.worker.command({"action": "forget_analysis", "analysis": "original"})
        refused = await self.worker.command(command)
        self.assertIs(expr1=AdmissionCode.INVALID_REFERENCE, expr2=refused["refusal"].code)
        with self.assertRaises(expected_exception=LookupError):
            await self.worker.command({
                "action": "observe", "analysis": "original", "operation": invocation.operation_id,
            })
        await self.worker.command({"action": "open_analysis", "analysis": "replacement"})
        await self.cleaned({"action": "close_analysis", "analysis": "replacement"})

    async def test_closed_analysis_keeps_evidence_until_ack_and_result_until_release(self):
        await self.start_worker(fail_initialization=False)
        reservation = await self.worker.command({"action": "open_analysis", "analysis": "original"})
        invocation = self.invocation(reservation=reservation)
        admitted = await self.worker.command({"action": "submit", "invocation": invocation})
        observed = await self.completed(operation=admitted["operation"])
        closed = await self.cleaned({"action": "close_analysis", "analysis": "original"})
        final = await self.worker.command({
            "action": "observe",
            "analysis": "original",
            "operation": invocation.operation_id,
        })
        self.assertEqual(first=observed.result, second=final.result)
        self.assertIs(expr1=ResultState.AVAILABLE, expr2=final.result_state)
        self.assertEqual(first=closed, second=await self.worker.command({
            "action": "close_analysis", "analysis": "original",
        }))
        with self.assertRaises(expected_exception=CapacityUnavailable):
            await self.worker.command({"action": "open_analysis", "analysis": "premature"})

        # Acknowledge transferred evidence before admitting another analysis.
        await self.worker.command({"action": "forget_analysis", "analysis": "original"})
        await self.worker.command({"action": "open_analysis", "analysis": "replacement"})
        request = {
            "analysis": "original",
            "operation": invocation.operation_id,
            "result": observed.result,
        }
        self.assertTrue(expr=await self.worker.command({"action": "read_result", **request}))
        released = await self.cleaned({"action": "release_result", **request})
        self.assertIs(expr1=ResultState.RELEASED, expr2=released["operation"].result_state)

        with self.assertRaises(expected_exception=LookupError):
            await self.worker.command({"action": "observe", **request})

        await self.cleaned({"action": "close_analysis", "analysis": "replacement"})

    async def test_forgetting_evidence_requires_successful_requested_cleanup(self):
        await self.start_worker(fail_initialization=False)
        await self.worker.command({"action": "open_analysis", "analysis": "original"})

        with self.assertRaises(expected_exception=RuntimeError):
            await self.worker.command({"action": "forget_analysis", "analysis": "original"})

        await self.cleaned({"action": "close_analysis", "analysis": "original"})
        self.assertIsNone(obj=await self.worker.command({
            "action": "forget_analysis", "analysis": "original",
        }))

        with self.assertRaises(expected_exception=LookupError):
            await self.worker.command({"action": "close_analysis", "analysis": "original"})

    async def test_unknown_analysis_close_is_not_cleanup_confirmation(self):
        await self.start_worker(fail_initialization=False)
        with self.assertRaises(expected_exception=LookupError):
            await self.worker.command({"action": "close_analysis", "analysis": "missing"})

    async def test_unencodable_reply_preserves_worker_without_repeating_command(self):
        requests = []

        async def command(worker, message):
            requests.append(message["action"])
            return object() if message["action"] == "unencodable" else len(requests)

        parent, child = socket.socketpair()
        parent.setblocking(False)

        with patch.object(_Worker, "command", new=command):
            serving = asyncio.create_task(_serve(child))
            try:
                async with asyncio.timeout(5):
                    await send_async(parent, {"action": "unencodable"})
                    failed = await receive_async(parent)
                    self.assertFalse(expr=failed["ok"])
                    self.assertIsInstance(obj=failed["error"], cls=RemoteScienceError)
                    self.assertIsInstance(obj=failed["error"].__cause__, cls=TypeError)

                    await send_async(parent, {"action": "retire"})
                    self.assertEqual(first={"ok": True, "value": 2}, second=await receive_async(parent))
                    await serving
            finally:
                parent.close()
                await asyncio.gather(serving, return_exceptions=True)

        self.assertEqual(first=["unencodable", "retire"], second=requests)


if __name__ == "__main__":
    unittest.main()
