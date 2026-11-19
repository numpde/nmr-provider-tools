"""Check process launch preflight before any child or socket effects."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import socket
import time
import unittest

from provider_core.hosting import (
    AcceptanceState, AdmissionCode, CleanupObservation, DispatchState,
    Invocation, NotAdmitted, Observation, OperationPolicy, Outcome,
    ResourceState, ResultRef, ResultState, observe_cleanup,
)
from provider_core.process_host import ProcessExecutionHost, _Channel, _ProcessAnalysis, _ProcessOperation
from provider_core.process_wire import MAX_PAYLOAD_BYTES, receive, receive_async, send, send_async
from provider_core.scientific import Contribution, HostedContribution, operation


@dataclass(frozen=True, slots=True, kw_only=True)
class Number:
    value: int


class DeclaredScience(Contribution):
    @operation()
    def calculate(self, *, number: Number) -> Number:
        return number


class ProcessChannelTests(unittest.TestCase):
    def test_local_framing_failure_sends_nothing_and_preserves_channel(self):
        (parent, child) = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        channel = _Channel(parent)

        with self.assertRaises(expected_exception=TypeError):
            channel.exchange({"action": "submit", "unsupported": object()})

        self.assertIsNone(channel.failure)
        child.settimeout(0.01)

        with self.assertRaises(expected_exception=TimeoutError):
            child.recv(1)


class ProcessHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_contribution_class_is_refused_before_child_spawn(self):
        class LocalContribution(Contribution):
            pass

        host = ProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=2,
            progress_records=1,
            initialization_seconds=2,
            retirement_seconds=2,
        )

        with self.assertRaises(expected_exception=ValueError):
            await host.start(
                contribution_type=LocalContribution,
                constructor="provider_core.scientific:Contribution",
                startup={},
                requirements=frozenset(),
            )

        self.assertIsNone(host.process)
        self.assertIsNone(host.channel)
        self.assertIsNone(host.stderr)


class ProcessOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = ProcessExecutionHost(
            input_bytes=4096,
            result_bytes=4096,
            state_values=2,
            progress_records=1,
            initialization_seconds=2,
            retirement_seconds=2,
        )
        self.parent, self.peer = socket.socketpair()
        self.host.channel = _Channel(self.parent)
        self.host.hosted = HostedContribution(
            contribution=None,
            contribution_type=DeclaredScience,
            host="test-host",
            allocation_name="test-allocation",
            requirements=frozenset(),
        )
        self.analysis = _ProcessAnalysis(self.host)
        self.host.active_analysis = self.analysis
        self.invocation = Invocation(
            target=self.host.hosted.reference(method=DeclaredScience.calculate),
            payload=b'{"value": 1}',
            states={},
            reservation=self.analysis.reservation_ref,
            policy=OperationPolicy(5, 5, 5, 1024, 1024),
        )

    async def asyncTearDown(self):
        self.peer.close()

        if self.host.exchanges:
            await asyncio.gather(*tuple(self.host.exchanges), return_exceptions=True)

        self.parent.close()
        self.host.exchange_executor.shutdown(wait=True)

    async def test_submission_owns_work_before_peer_receipt_and_never_repeats_bad_acknowledgement(self):
        unsupported = replace(
            self.invocation,
            policy=replace(self.invocation.policy, reject_encoded_result_over_bytes=MAX_PAYLOAD_BYTES + 1),
        )
        refusal = self.analysis.submit(unsupported)
        self.assertIs(type(refusal), NotAdmitted)
        self.assertIs(AdmissionCode.UNSUPPORTED, refusal.code)

        def reply_with_wrong_identity():
            request = receive(self.peer, deadline=time.monotonic() + 5)
            owned_at_receipt = self.analysis.operations[request["invocation"].operation_id]
            send(self.peer, {"ok": True, "value": {"operation": "another-operation"}})
            return owned_at_receipt

        with ThreadPoolExecutor(max_workers=1) as peer_executor:
            receipt = peer_executor.submit(reply_with_wrong_identity)
            original = self.analysis.submit(self.invocation)
            self.assertIs(original, receipt.result(timeout=5))

        self.assertIs(original, self.analysis.submit(self.invocation))
        self.assertIsNotNone(self.host.channel.failure)
        self.peer.setblocking(False)

        try:
            additional_bytes = self.peer.recv(1)
        except BlockingIOError:
            additional_bytes = b""

        self.assertEqual(b"", additional_bytes)

    async def test_close_starts_without_observation_and_survives_cancelled_observer(self):
        self.peer.setblocking(False)
        cleanup = self.analysis.close()
        request = await asyncio.wait_for(receive_async(self.peer), timeout=5)
        self.assertEqual("close_analysis", request["action"])
        observing = asyncio.Event()

        async def observe_original_cleanup():
            observing.set()
            return await observe_cleanup(cleanup)

        observer = asyncio.create_task(observe_original_cleanup())
        await observing.wait()
        observer.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await observer

        self.assertFalse(cleanup.task.done())
        await send_async(self.peer, {
            "ok": True,
            "value": CleanupObservation(1, True, False, ()),
        })
        request = await asyncio.wait_for(receive_async(self.peer), timeout=5)
        self.assertEqual("forget_analysis", request["action"])
        await send_async(self.peer, {"ok": True, "value": None})
        await asyncio.wait_for(asyncio.shield(cleanup.task), timeout=5)
        observed = await cleanup.observe(after=None, wait_seconds=0)
        self.assertTrue(observed.settled)
        self.assertEqual((), observed.failures)
        self.assertIs(cleanup, self.analysis.close())
        self.assertIsNone(self.host.active_analysis)

    async def test_close_and_release_transfer_final_evidence_after_original_observation(self):
        self.peer.setblocking(False)
        original = _ProcessOperation(self.analysis, self.invocation, acknowledged=True)
        self.analysis.operations[self.invocation.operation_id] = original
        result = ResultRef(original.reference, "known-result")
        completed = Observation(
            operation=original.reference,
            target=self.invocation.target,
            revision=2,
            acceptance=AcceptanceState.OWNED,
            dispatch=DispatchState.STARTED,
            finished=True,
            outcome=Outcome.SUCCEEDED,
            result_state=ResultState.AVAILABLE,
            result=result,
            states=(),
            progress=(),
            progress_dropped=0,
            failures=(),
            stop_requested=False,
            allowance_expired=False,
            resources=ResourceState.OCCUPIED,
        )
        original.last_observation = completed

        # Hold an original reply while both cleanup owners are created. Their
        # ordering must drain that exchange without making it wait for them.
        earlier_observer = asyncio.create_task(original.observe(after=None, wait_seconds=0))
        request = await asyncio.wait_for(receive_async(self.peer), timeout=5)
        self.assertEqual("observe", request["action"])
        release = original.release_result(result)
        cleanup = self.analysis.close()
        await send_async(self.peer, {"ok": True, "value": completed})
        self.assertEqual(completed, await asyncio.wait_for(earlier_observer, timeout=5))

        request = await asyncio.wait_for(receive_async(self.peer), timeout=5)
        self.assertEqual("release_result", request["action"])
        later_started = asyncio.Event()

        async def observe_after_close():
            later_started.set()
            return await original.observe(after=None, wait_seconds=0)

        later_observer = asyncio.create_task(observe_after_close())
        await later_started.wait()
        self.assertFalse(later_observer.done())
        released = replace(completed, revision=3, result_state=ResultState.RELEASED)
        await send_async(self.peer, {
            "ok": True,
            "value": {"cleanup": CleanupObservation(1, True, False, ()), "operation": released},
        })

        request = await asyncio.wait_for(receive_async(self.peer), timeout=5)
        self.assertEqual("close_analysis", request["action"])
        await send_async(self.peer, {"ok": True, "value": CleanupObservation(2, True, False, ())})
        request = await asyncio.wait_for(receive_async(self.peer), timeout=5)
        self.assertEqual("observe", request["action"])
        final = replace(released, revision=4, resources=ResourceState.RELEASED)
        await send_async(self.peer, {"ok": True, "value": final})
        request = await asyncio.wait_for(receive_async(self.peer), timeout=5)
        self.assertEqual("forget_analysis", request["action"])
        await send_async(self.peer, {"ok": True, "value": None})

        self.assertEqual(final, await asyncio.wait_for(later_observer, timeout=5))
        self.assertTrue((await observe_cleanup(release)).settled)
        self.assertTrue((await observe_cleanup(cleanup)).settled)
        self.assertEqual(final, await original.observe(after=None, wait_seconds=0))
        self.assertIsNone(self.host.active_analysis)
        self.assertIsNone(self.host.channel.failure)

        with self.assertRaises(expected_exception=BlockingIOError):
            self.peer.recv(1)

    async def test_cancelled_observation_retains_reply_before_subsequent_channel_loss(self):
        self.peer.setblocking(False)
        original = _ProcessOperation(self.analysis, self.invocation, acknowledged=True)
        self.analysis.operations[self.invocation.operation_id] = original
        observer = asyncio.create_task(original.observe(after=None, wait_seconds=0))
        request = await asyncio.wait_for(receive_async(self.peer), timeout=5)
        self.assertEqual("observe", request["action"])
        observer.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await observer

        result = ResultRef(original.reference, "known-result")
        completed = Observation(
            operation=original.reference,
            target=self.invocation.target,
            revision=2,
            acceptance=AcceptanceState.OWNED,
            dispatch=DispatchState.STARTED,
            finished=True,
            outcome=Outcome.SUCCEEDED,
            result_state=ResultState.AVAILABLE,
            result=result,
            states=(),
            progress=(),
            progress_dropped=0,
            failures=(),
            stop_requested=False,
            allowance_expired=False,
            resources=ResourceState.OCCUPIED,
        )
        await send_async(self.peer, {"ok": True, "value": completed})
        await asyncio.wait_for(asyncio.shield(original.observation_task), timeout=5)
        self.peer.close()
        observed = await original.observe(after=None, wait_seconds=0)

        self.assertIs(Outcome.SUCCEEDED, observed.outcome)
        self.assertEqual(result, observed.result)
        self.assertIs(ResultState.AVAILABLE, observed.result_state)
        self.assertEqual("backend", observed.failures[-1].phase)


if __name__ == "__main__":
    unittest.main()
