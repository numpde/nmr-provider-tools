"""Exercise scientific request ownership and native acquisition preflight."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
import socket
import tempfile
import time
import unittest

from provider_core.hosting import (
    AcceptanceState,
    AdmissionCode,
    CleanupObservation,
    DispatchState,
    Invocation,
    NotAdmitted,
    Observation,
    OperationPolicy,
    Outcome,
    ResourceState,
    ResultRef,
    ResultState,
    observe_cleanup,
)
from provider_core.process_host import (
    ProcessExecutionHost,
    _Channel,
    _ProcessAnalysis,
    _ProcessOperation,
)
from provider_core.process_wire import MAX_PAYLOAD_BYTES, receive, receive_async, send, send_async
from provider_core.runner_session import RunnerSession
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
        (parent_science_socket, peer_science_socket) = socket.socketpair()

        self.addCleanup(parent_science_socket.close)
        self.addCleanup(peer_science_socket.close)

        channel = _Channel(parent_science_socket)

        with self.assertRaises(expected_exception=TypeError):
            channel.exchange({"action": "submit", "unsupported": object()})

        self.assertIsNone(obj=channel.failure)

        peer_science_socket.settimeout(0.01)

        with self.assertRaises(expected_exception=TimeoutError):
            peer_science_socket.recv(1)


class ProcessHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_contribution_class_is_refused_before_native_acquisition(self):
        class LocalContribution(Contribution):
            pass

        with tempfile.TemporaryDirectory(prefix="scientific-host-preflight-") as temporary_root:
            session = RunnerSession(
                Path(temporary_root) / "unused.sock",
                role="scientific-test",
                expected_lock_identity=(0, 0),
                startup_seconds=2,
                retirement_seconds=2,
            )

            async with ProcessExecutionHost(
                session=session,
                input_bytes=4096,
                result_bytes=4096,
                state_values=2,
                progress_records=1,
                initialization_seconds=2,
                retirement_seconds=2,
            ) as host:
                with self.assertRaises(expected_exception=ValueError):
                    await host.start(
                        contribution_type=LocalContribution,
                        constructor="provider_core.scientific:Contribution",
                        startup={},
                        requirements=frozenset(),
                    )

                self.assertFalse(expr=session.control_connected)
                self.assertFalse(expr=session._connect_started)
                self.assertIsNone(obj=session.operation)
                self.assertIsNone(obj=session.science)
                self.assertIsNone(obj=host.channel)

            self.assertEqual(first=-1, second=session.control.fileno())


class ProcessOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="scientific-host-channel-")
        self.session = RunnerSession(
            Path(self.temporary.name) / "unused.sock",
            role="scientific-test",
            expected_lock_identity=(0, 0),
            startup_seconds=2,
            retirement_seconds=2,
        )
        self.host = ProcessExecutionHost(
            session=self.session,
            input_bytes=4096,
            result_bytes=4096,
            state_values=2,
            progress_records=1,
            initialization_seconds=2,
            retirement_seconds=2,
        )
        (self.parent_science_socket, self.peer_science_socket) = socket.socketpair()
        self.host.channel = _Channel(self.parent_science_socket)
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
        self.peer_science_socket.close()

        if self.host.cleanups:
            await asyncio.gather(*tuple(self.host.cleanups), return_exceptions=True)

        if self.host.exchanges:
            await asyncio.gather(*tuple(self.host.exchanges), return_exceptions=True)

        self.parent_science_socket.close()
        self.host.exchange_executor.shutdown(wait=True)
        self.session.close()
        self.temporary.cleanup()

    async def test_submission_owns_work_before_peer_receipt_and_never_repeats_bad_acknowledgement(self):
        unsupported = replace(
            self.invocation,
            policy=replace(self.invocation.policy, reject_encoded_result_over_bytes=MAX_PAYLOAD_BYTES + 1),
        )
        refusal = self.analysis.submit(unsupported)

        self.assertIs(expr1=NotAdmitted, expr2=type(refusal))
        self.assertIs(expr1=AdmissionCode.UNSUPPORTED, expr2=refusal.code)

        def reply_with_wrong_identity():
            worker_command = receive(self.peer_science_socket, deadline=time.monotonic() + 5)
            owned_at_receipt = self.analysis.operations[worker_command["invocation"].operation_id]

            send(self.peer_science_socket, {"ok": True, "value": {"operation": "another-operation"}})

            return owned_at_receipt

        with ThreadPoolExecutor(max_workers=1) as peer_executor:
            peer_receipt = peer_executor.submit(reply_with_wrong_identity)
            original = self.analysis.submit(self.invocation)

            self.assertIs(expr1=original, expr2=peer_receipt.result(timeout=5))

        self.assertIs(expr1=original, expr2=self.analysis.submit(self.invocation))
        self.assertIsNotNone(obj=self.host.channel.failure)

        self.peer_science_socket.setblocking(False)

        try:
            additional_bytes = self.peer_science_socket.recv(1)
        except BlockingIOError:
            additional_bytes = b""

        self.assertEqual(first=b"", second=additional_bytes)

    async def test_close_starts_without_observation_and_survives_cancelled_observer(self):
        self.peer_science_socket.setblocking(False)

        cleanup = self.analysis.close()
        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="close_analysis", second=worker_command["action"])

        observing = asyncio.Event()

        async def observe_original_cleanup():
            observing.set()

            return await observe_cleanup(cleanup)

        observer = asyncio.create_task(observe_original_cleanup())

        await observing.wait()
        observer.cancel()

        with self.assertRaises(expected_exception=asyncio.CancelledError):
            await observer

        self.assertFalse(expr=cleanup.task.done())

        await send_async(
            self.peer_science_socket,
            {
                "ok": True,
                "value": CleanupObservation(1, True, False, ()),
            },
        )

        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="forget_analysis", second=worker_command["action"])

        await send_async(self.peer_science_socket, {"ok": True, "value": None})
        await asyncio.wait_for(asyncio.shield(cleanup.task), timeout=5)

        observed = await cleanup.observe(after=None, wait_seconds=0)

        self.assertTrue(expr=observed.settled)
        self.assertEqual(first=(), second=observed.failures)
        self.assertIs(expr1=cleanup, expr2=self.analysis.close())
        self.assertIsNone(obj=self.host.active_analysis)

    async def test_close_and_release_transfer_final_evidence_after_original_observation(self):
        self.peer_science_socket.setblocking(False)

        original = _ProcessOperation(self.analysis, self.invocation, acknowledged=True)
        self.analysis.operations[self.invocation.operation_id] = original
        result_ref = ResultRef(original.reference, "known-result")
        completed = Observation(
            operation=original.reference,
            target=self.invocation.target,
            revision=2,
            acceptance=AcceptanceState.OWNED,
            dispatch=DispatchState.STARTED,
            finished=True,
            outcome=Outcome.SUCCEEDED,
            result_state=ResultState.AVAILABLE,
            result=result_ref,
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
        earlier_observer = asyncio.create_task(original.observe(after=None, wait_seconds=5))
        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="observe", second=worker_command["action"])

        release = original.release_result(result_ref)
        cleanup = self.analysis.close()

        await send_async(self.peer_science_socket, {"ok": True, "value": completed})
        self.assertEqual(first=completed, second=await asyncio.wait_for(earlier_observer, timeout=5))

        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="release_result", second=worker_command["action"])

        later_started = asyncio.Event()

        async def observe_after_close():
            later_started.set()

            return await original.observe(after=None, wait_seconds=5)

        later_observer = asyncio.create_task(observe_after_close())

        await later_started.wait()
        self.assertFalse(expr=later_observer.done())

        released = replace(completed, revision=3, result_state=ResultState.RELEASED)

        await send_async(
            self.peer_science_socket,
            {
                "ok": True,
                "value": {"cleanup": CleanupObservation(1, True, False, ()), "operation": released},
            },
        )

        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="close_analysis", second=worker_command["action"])

        await send_async(self.peer_science_socket, {"ok": True, "value": CleanupObservation(2, True, False, ())})

        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="observe", second=worker_command["action"])

        final = replace(released, revision=4, resources=ResourceState.RELEASED)

        await send_async(self.peer_science_socket, {"ok": True, "value": final})

        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="forget_analysis", second=worker_command["action"])

        await send_async(self.peer_science_socket, {"ok": True, "value": None})

        self.assertEqual(first=final, second=await asyncio.wait_for(later_observer, timeout=5))
        self.assertTrue(expr=(await observe_cleanup(release)).settled)
        self.assertTrue(expr=(await observe_cleanup(cleanup)).settled)
        self.assertEqual(first=final, second=await original.observe(after=None, wait_seconds=0))
        self.assertIsNone(obj=self.host.active_analysis)
        self.assertIsNone(obj=self.host.channel.failure)

        with self.assertRaises(expected_exception=BlockingIOError):
            self.peer_science_socket.recv(1)

    async def test_zero_wait_returns_pending_facts_without_abandoning_the_remote_reply(self):
        self.peer_science_socket.setblocking(False)

        operation = _ProcessOperation(self.analysis, self.invocation, acknowledged=True)
        self.analysis.operations[self.invocation.operation_id] = operation

        # No peer reply is available. Zero-wait observation must nevertheless
        # return; the original remote request must remain owned.
        initial = await asyncio.wait_for(
            operation.observe(after=None, wait_seconds=0),
            timeout=0.5,
        )

        self.assertEqual(first=0, second=initial.revision)
        self.assertIs(expr1=AcceptanceState.OWNED, expr2=initial.acceptance)
        self.assertIs(expr1=DispatchState.UNKNOWN, expr2=initial.dispatch)
        self.assertFalse(expr=initial.finished)

        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="observe", second=worker_command["action"])

        # A bounded later observer is also free to leave while the reply is
        # delayed; neither call sends another request.
        still_pending = await asyncio.wait_for(
            operation.observe(after=initial.revision, wait_seconds=0.01),
            timeout=0.5,
        )

        self.assertEqual(first=initial, second=still_pending)
        self.assertIsNotNone(obj=operation.observation_task)
        self.assertFalse(expr=operation.observation_task.done())

        completed = replace(
            initial,
            revision=1,
            finished=True,
            outcome=Outcome.REJECTED,
        )

        await send_async(self.peer_science_socket, {"ok": True, "value": completed})

        observed = await operation.observe(after=initial.revision, wait_seconds=1)

        self.assertEqual(first=completed, second=observed)
        self.assertIsNone(obj=self.host.channel.failure)

        with self.assertRaises(expected_exception=BlockingIOError):
            self.peer_science_socket.recv(1)

        with self.assertRaisesRegex(expected_exception=ValueError, expected_regex="cursor"):
            await operation.observe(after=observed.revision + 1, wait_seconds=0)

    async def test_cancelled_observation_retains_reply_before_subsequent_channel_loss(self):
        self.peer_science_socket.setblocking(False)

        original = _ProcessOperation(self.analysis, self.invocation, acknowledged=True)
        self.analysis.operations[self.invocation.operation_id] = original
        observer = asyncio.create_task(original.observe(after=None, wait_seconds=5))
        worker_command = await asyncio.wait_for(receive_async(self.peer_science_socket), timeout=5)

        self.assertEqual(first="observe", second=worker_command["action"])

        observer.cancel()

        with self.assertRaises(expected_exception=asyncio.CancelledError):
            await observer

        result_ref = ResultRef(original.reference, "known-result")
        completed = Observation(
            operation=original.reference,
            target=self.invocation.target,
            revision=2,
            acceptance=AcceptanceState.OWNED,
            dispatch=DispatchState.STARTED,
            finished=True,
            outcome=Outcome.SUCCEEDED,
            result_state=ResultState.AVAILABLE,
            result=result_ref,
            states=(),
            progress=(),
            progress_dropped=0,
            failures=(),
            stop_requested=False,
            allowance_expired=False,
            resources=ResourceState.OCCUPIED,
        )

        await send_async(self.peer_science_socket, {"ok": True, "value": completed})
        await asyncio.wait_for(asyncio.shield(original.observation_task), timeout=5)
        self.peer_science_socket.close()

        observed = await original.observe(after=None, wait_seconds=5)

        self.assertIs(expr1=Outcome.SUCCEEDED, expr2=observed.outcome)
        self.assertEqual(first=result_ref, second=observed.result)
        self.assertIs(expr1=ResultState.AVAILABLE, expr2=observed.result_state)
        self.assertEqual(first="backend", second=observed.failures[-1].phase)


if __name__ == "__main__":
    unittest.main()
