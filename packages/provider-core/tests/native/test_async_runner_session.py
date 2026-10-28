"""Caller waits retain installed native owners until actual cleanup or read failure."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from provider_core.async_runner_session import AsyncRunnerSession
from provider_core.runner_protocol import LifecycleRejected, send_greeting
from provider_core.runner_session import RunnerSession, RunnerStartupUnavailable
from tests.fakes.runner import RunningSupervisor, worker_pid


class AsyncNativeProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = RunningSupervisor(operation_seconds=None)
        self.addCleanup(self.runner.close)
        self.session = self.runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(self.session.close)
        self.owner = AsyncRunnerSession(self.session)
        await self.owner.connect(wait_seconds=2)

    async def asyncTearDown(self):
        # Containment after assertions cannot substitute for their retirement proof.
        if self.session.retirement is None:
            self.owner.request_stop()
            await self.owner.join_retirement(wait_seconds=2)

    def assert_retired(self, pid):
        self.assertTrue(self.session.retirement.scratch_released)
        self.assertEqual([], list(self.runner.scratch.iterdir()))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIsNone(self.runner.process.poll())

    async def test_eof_with_live_child_observes_then_stops_and_joins_original_reader(self):
        pid = await asyncio.to_thread(worker_pid, self.session.science, b"E")
        self.assertEqual(b"", await asyncio.to_thread(self.session.science.recv, 1))
        os.kill(pid, 0)
        identity = self.session.operation, self.session.lock_identity
        self.assertIsNone(self.owner.retirement_deadline)
        with self.assertRaises(TimeoutError):
            await self.owner.observe_retirement(wait_seconds=0.05)
        original = self.owner.reader
        deadline = self.owner.retirement_deadline
        self.assertIsNotNone(deadline)
        self.assertFalse(original.done())
        self.assertIsNone(self.session._observation_failure)
        self.owner.request_stop()
        await self.owner.join_retirement(wait_seconds=2)
        self.assertIs(original, self.owner.reader)
        self.assertEqual(deadline, self.owner.retirement_deadline)
        self.assertEqual(identity, (self.session.operation, self.session.lock_identity))
        self.assertEqual(-signal.SIGKILL, self.session.retirement.returncode)
        self.assertEqual("owner_lost", self.session.retirement.reason)
        self.assertIsNone(self.session.original_exit)
        self.assert_retired(pid)

    async def test_cancelled_observer_cannot_cancel_original_reader_or_prevent_cleanup(self):
        pid = await asyncio.to_thread(worker_pid, self.session.science, b"H")
        observer = asyncio.create_task(self.owner.observe_retirement(wait_seconds=2))
        async with asyncio.timeout(1):
            while self.owner.reader is None:
                await asyncio.sleep(0)
        original = self.owner.reader
        observer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await observer
        self.assertFalse(original.done())
        self.owner.request_stop()
        await self.owner.join_retirement(wait_seconds=2)
        self.assertIs(original, self.owner.reader)
        self.assertFalse(original.cancelled())
        self.assertIsNone(self.session.original_exit)
        self.assert_retired(pid)

    async def test_original_child_exit_survives_later_stop_and_repeated_join(self):
        pid = await asyncio.to_thread(worker_pid, self.session.science)
        await self.owner.observe_retirement(wait_seconds=2)
        original_exit, reader = self.session.original_exit, self.owner.reader
        self.assertEqual(0, original_exit.returncode)
        self.owner.request_stop()
        await self.owner.join_retirement(wait_seconds=2)
        self.assertIs(original_exit, self.session.original_exit)
        self.assertIs(reader, self.owner.reader)
        self.assert_retired(pid)

    async def test_stop_failure_stays_independent_of_the_original_reader(self):
        pid = await asyncio.to_thread(worker_pid, self.session.science, b"H")
        with self.assertRaises(TimeoutError):
            await self.owner.observe_retirement(wait_seconds=0.05)
        original = self.owner.reader
        failure = OSError("Injected native stop failure")
        with patch.object(self.session, "request_stop", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                self.owner.request_stop()
            self.assertIs(failure, caught.exception)
        self.owner.request_stop()
        await self.owner.join_retirement(wait_seconds=2)
        self.assertIs(failure, self.owner.stop_failure)
        self.assertIs(original, self.owner.reader)
        self.assert_retired(pid)

    async def test_control_setup_and_actual_child_retirement_survive_a_saturated_default_pool(self):
        default = ThreadPoolExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(default)
        entered, release = threading.Event(), threading.Event()

        def occupy_default():
            entered.set()
            release.wait()

        blocker = default.submit(occupy_default)
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0)
            pid = worker_pid(self.session.science, b"H")
            self.owner.request_stop()
            await self.owner.join_retirement(wait_seconds=2)
            self.assert_retired(pid)
            successor = self.runner.session(startup_seconds=2, retirement_seconds=2)
            self.addCleanup(successor.close)
            owner = AsyncRunnerSession(successor)
            await owner.connect(wait_seconds=2)
            self.assertFalse(blocker.done())
            self.assertIsInstance(owner.setup, Future)
            pid = worker_pid(successor.science)
            await owner.observe_retirement(wait_seconds=2)
            self.assertIsInstance(owner.reader, Future)
            self.assertFalse(blocker.done())
            self.assertTrue(successor.retirement.scratch_released)
            self.assertEqual([], list(self.runner.scratch.iterdir()))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            release.set()
            default.shutdown(wait=True)

    async def test_waiter_timeout_and_cancellation_do_not_release_pending_control_executor(self):
        pid = worker_pid(self.session.science, b"H")
        executor = self.owner._executor
        wrapped = []
        wrap = asyncio.wrap_future

        def capture(original):
            notification = wrap(original)
            wrapped.append(notification)
            return notification

        with patch.object(executor, "shutdown", wraps=executor.shutdown) as shutdown, \
                patch("provider_core.async_runner_session.asyncio.wrap_future", side_effect=capture):
            with self.assertRaises(TimeoutError):
                await self.owner.observe_retirement(wait_seconds=0.01)
            original = self.owner.reader
            self.assertFalse(original.done())
            shutdown.assert_not_called()
            self.assertFalse(wrapped[-1].done())
            observer = asyncio.create_task(self.owner.join_retirement(wait_seconds=2))
            await asyncio.sleep(0)
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await observer
            shutdown.assert_not_called()
            self.assertFalse(original.done())
            self.assertFalse(wrapped[-1].done())
            self.owner.request_stop()
            await self.owner.join_retirement(wait_seconds=2)
            async with asyncio.timeout(1):
                while not shutdown.called:
                    await asyncio.sleep(0)
            shutdown.assert_called_once_with(wait=False)
        for thread in executor._threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assert_retired(pid)

    async def test_warm_child_outlives_setup_cap_and_keeps_its_later_retirement_allowance(self):
        self.owner.request_stop()
        await self.owner.join_retirement(wait_seconds=2)
        native = self.runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(native.close)
        cap = time.monotonic() + 1
        owner = AsyncRunnerSession(native, setup_not_after=cap)
        await owner.connect(wait_seconds=2)
        pid = worker_pid(native.science, b"H")
        await asyncio.sleep(max(0, cap - time.monotonic()))
        self.assertIsNone(owner.retirement_deadline)
        os.kill(pid, 0)
        owner.request_stop()
        await owner.join_retirement(wait_seconds=2)
        self.assertGreater(owner.retirement_deadline, cap)
        self.assertTrue(native.retirement.scratch_released)
        self.assertIsNone(native.original_exit)
        self.assertEqual([], list(self.runner.scratch.iterdir()))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class AsyncNativeWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="async-native-wire-")
        self.addCleanup(self.temporary.cleanup)
        self.listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.listener.close)
        self.path = str(Path(self.temporary.name) / "runner.sock")
        self.listener.bind(self.path)
        self.listener.listen()
        self.listener.settimeout(2)
        self.session = RunnerSession(self.path, role="fixture", expected_lock_identity=(0, 0),
                                     startup_seconds=2, retirement_seconds=0.3)
        self.addCleanup(self.session.close)
        self.owner = AsyncRunnerSession(self.session)

    async def assert_native_failure(self, cause, *, setup):
        if not setup:
            await self.connect()
        caller_thread = threading.get_ident()
        native_threads = []
        release = threading.Event()

        def native_failure(deadline):
            native_threads.append(threading.get_ident())
            if not setup and not release.wait(1):
                raise TimeoutError("Fixture did not release the retained native read")
            raise cause

        if setup:
            hook, observe = "_connect_control", self.owner.connect
        else:
            hook, observe = "_receive_retirement", self.owner.observe_retirement
        with patch.object(self.session, hook, new=native_failure):
            if not setup:
                try:
                    with self.assertRaises(TimeoutError):
                        await observe(wait_seconds=0.01)
                    self.assertFalse(self.owner.reader.done())
                finally:
                    release.set()
                # No observer remains when the native function throws. Its
                # physical Future must retain the cause without a Task raising it.
                async with asyncio.timeout(1):
                    while not self.owner.reader.done():
                        await asyncio.sleep(0)
            with self.assertRaises(type(cause)) as first:
                await observe(wait_seconds=1)
            original = self.owner.setup if setup else self.owner.reader
            self.assertIs(cause, first.exception)
            self.assertFalse(original.cancelled())
            self.assertIs(cause, original.exception())
            for _ in range(2):
                with self.assertRaises(type(cause)) as joined:
                    await self.owner.join_retirement(wait_seconds=1)
                self.assertIs(cause, joined.exception)
        self.assertEqual(1, len(native_threads))
        self.assertNotEqual(caller_thread, native_threads[0])
        self.assertEqual(0, asyncio.current_task().cancelling())
        self.assertIs(cause, self.owner.setup_failure if setup else self.owner.retirement_failure)

    async def test_native_setup_cancelled_error_preserves_identity_on_repeated_join(self):
        await self.assert_native_failure(asyncio.CancelledError("native setup cancellation"), setup=True)

    async def test_native_setup_keyboard_interrupt_remains_an_observed_outcome(self):
        await self.assert_native_failure(KeyboardInterrupt("native setup interrupt"), setup=True)

    async def test_native_setup_system_exit_remains_an_observed_outcome(self):
        await self.assert_native_failure(SystemExit("native setup exit"), setup=True)

    async def test_native_read_cancelled_error_preserves_identity_on_repeated_join(self):
        await self.assert_native_failure(asyncio.CancelledError("native read cancellation"), setup=False)

    async def test_native_read_keyboard_interrupt_remains_an_observed_outcome(self):
        await self.assert_native_failure(KeyboardInterrupt("native read interrupt"), setup=False)

    async def test_native_read_system_exit_remains_an_observed_outcome(self):
        await self.assert_native_failure(SystemExit("native read exit"), setup=False)

    async def test_inherited_cap_requires_positive_finite_absolute_seconds(self):
        for cap in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                AsyncRunnerSession(self.session, not_after=cap)
            with self.subTest(setup_cap=cap), self.assertRaises(ValueError):
                AsyncRunnerSession(self.session, setup_not_after=cap)

    async def test_expired_inherited_cap_never_connects_or_starts_a_control_read(self):
        self.owner = AsyncRunnerSession(self.session, not_after=time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            await self.owner.connect(wait_seconds=1)
        async with asyncio.timeout(1):
            while not self.owner.setup.done():
                await asyncio.sleep(0)
        cause = self.owner.setup_failure
        self.assertIsInstance(cause, RunnerStartupUnavailable)
        self.assertFalse(self.session.control_connected)
        self.listener.setblocking(False)
        with self.assertRaises(BlockingIOError):
            self.listener.accept()
        with self.assertRaises(RunnerStartupUnavailable) as joined:
            await self.owner.join_retirement(wait_seconds=1)
        self.assertIs(cause, joined.exception)
        self.assertIsNone(self.owner.reader)

    async def test_inherited_cap_bounds_the_original_setup_read(self):
        cap = time.monotonic() + 0.08
        self.owner = AsyncRunnerSession(self.session, not_after=cap, setup_not_after=cap + 1)
        opening = asyncio.create_task(self.owner.connect(wait_seconds=1))
        peer, _ = await asyncio.to_thread(self.listener.accept)
        self.addCleanup(peer.close)
        # Keep a real connected peer alive without supplying the greeting.
        with self.assertRaises(TimeoutError):
            await opening
        async with asyncio.timeout(1):
            while not self.owner.setup.done():
                await asyncio.sleep(0)
        cause = self.owner.setup_failure
        self.assertIsInstance(cause, TimeoutError)
        self.assertIs(cause, self.session._observation_failure)
        self.assertTrue(self.session.control_connected)
        self.owner.request_stop()
        self.assertEqual(cap, self.owner.retirement_deadline)
        with self.assertRaises(TimeoutError) as joined:
            await self.owner.join_retirement(wait_seconds=1)
        self.assertIs(cause, joined.exception)
        self.assertIsNone(self.owner.reader)

    async def test_inherited_cap_survives_setup_observation_stop_and_repeated_read_join(self):
        cap = time.monotonic() + 0.15
        self.owner = AsyncRunnerSession(self.session, not_after=cap)
        await self.connect(greeting_delay=0.04)
        with self.assertRaises(TimeoutError):
            await self.owner.observe_retirement(wait_seconds=0.01)
        original = self.owner.reader
        self.assertEqual(cap, self.owner.retirement_deadline)
        self.owner.request_stop()
        with self.assertRaises(TimeoutError):
            await self.owner.join_retirement(wait_seconds=1)
        async with asyncio.timeout(1):
            while not original.done():
                await asyncio.sleep(0)
        cause = self.owner.retirement_failure
        self.assertIsInstance(cause, TimeoutError)
        self.assertIs(cause, self.session._observation_failure)
        with self.assertRaises(TimeoutError) as joined:
            await self.owner.join_retirement(wait_seconds=1)
        self.assertIs(cause, joined.exception)
        self.assertIs(original, self.owner.reader)
        self.assertEqual(cap, self.owner.retirement_deadline)

    async def test_rejected_native_submission_releases_executor_without_an_original_operation(self):
        failure = RuntimeError("Injected native submission failure")
        executor = self.owner._executor
        with patch.object(executor, "submit", side_effect=failure), \
                patch.object(executor, "shutdown", wraps=executor.shutdown) as shutdown:
            with self.assertRaises(RuntimeError) as caught:
                await self.owner.connect(wait_seconds=1)
            shutdown.assert_called_once_with(wait=False)
        self.assertIs(failure, caught.exception)
        self.assertIsNone(self.owner.setup)
        self.assertIsNone(self.owner.retirement_deadline)
        self.assertFalse(self.session.control_connected)

    async def test_cancelled_original_future_is_unconfirmed_control_not_native_exit(self):
        entered, release = threading.Event(), threading.Event()

        def occupy_control():
            entered.set()
            release.wait()

        executor = self.owner._executor
        blocker = executor.submit(occupy_control)
        opening = asyncio.create_task(self.owner.connect(wait_seconds=1))
        try:
            async with asyncio.timeout(1):
                while not entered.is_set() or self.owner.setup is None:
                    await asyncio.sleep(0)
            self.assertTrue(self.owner.setup.cancel())
            with self.assertRaises(LifecycleRejected) as failed:
                await opening
            self.assertIn("unconfirmed", str(failed.exception))
            self.assertIsInstance(self.owner.setup_failure, LifecycleRejected)
            self.assertFalse(self.session.control_connected)
            self.assertIsNone(self.session.original_exit)
            with self.assertRaises(LifecycleRejected):
                await self.owner.join_retirement(wait_seconds=1)
        finally:
            release.set()
            blocker.result(timeout=1)

    async def test_delayed_setup_dispatch_cannot_connect_after_its_original_setup_cap(self):
        cap = time.monotonic() + 0.04
        self.owner = AsyncRunnerSession(self.session, setup_not_after=cap)
        entered, release = threading.Event(), threading.Event()

        def occupy_control():
            entered.set()
            release.wait()

        blocker = self.owner._executor.submit(occupy_control)
        try:
            with self.assertRaises(TimeoutError):
                await self.owner.connect(wait_seconds=0.01)
            original = self.owner.setup
            self.assertFalse(original.done())
            await asyncio.sleep(max(0, cap - time.monotonic()))
        finally:
            release.set()
            blocker.result(timeout=1)
        async with asyncio.timeout(1):
            while not original.done():
                await asyncio.sleep(0)
        cause = self.owner.setup_failure
        self.assertIsInstance(cause, RunnerStartupUnavailable)
        self.assertFalse(self.session.control_connected)
        self.listener.setblocking(False)
        with self.assertRaises(BlockingIOError):
            self.listener.accept()
        with self.assertRaises(RunnerStartupUnavailable) as repeated:
            await self.owner.connect(wait_seconds=1)
        self.assertIs(cause, repeated.exception)
        self.assertIs(original, self.owner.setup)

    # silent-defaults: allow-contract -- Ordinary wire fixtures greet immediately; the inherited-cap witness delays that same greeting.
    async def connect(self, *, greeting_delay=0):
        opening = asyncio.create_task(self.owner.connect(wait_seconds=2))
        self.peer, _ = await asyncio.to_thread(self.listener.accept)
        self.addCleanup(self.peer.close)
        science, remote = socket.socketpair()
        self.addCleanup(remote.close)
        if greeting_delay:
            await asyncio.sleep(greeting_delay)
        with science:
            send_greeting(self.peer, {"kind": "opened", "role": "fixture", "lock": [0, 0],
                                     "generation": "1" * 32, "operation": "2" * 32},
                          science, deadline=time.monotonic() + 2)
        await opening

    def retirement_frame(self):
        raw = json.dumps({"kind": "retired", "generation": "1" * 32, "operation": "2" * 32,
                          "returncode": 0, "scratch_released": True, "reason": "exited"}).encode()
        return struct.pack("!I", len(raw)) + raw

    async def test_partial_frame_continues_in_same_reader_after_observer_timeout(self):
        await self.connect()
        frame = self.retirement_frame()
        self.peer.sendall(frame[:6])
        with self.assertRaises(TimeoutError):
            await self.owner.observe_retirement(wait_seconds=0.02)
        original = self.owner.reader
        self.assertFalse(original.done())
        self.peer.sendall(frame[6:])
        await self.owner.join_retirement(wait_seconds=1)
        self.assertIs(original, self.owner.reader)
        self.assertIsNone(self.session._observation_failure)

    async def test_real_partial_read_timeout_preserves_original_cause_and_never_restarts(self):
        await self.connect()
        self.peer.sendall(self.retirement_frame()[:2])
        with self.assertRaises(TimeoutError):
            await self.owner.observe_retirement(wait_seconds=1)
        original = self.owner.reader
        # The aggregate waiter and actual socket deadline may become ready together.
        async with asyncio.timeout(1):
            while not original.done():
                await asyncio.sleep(0)
        cause = original.exception()
        self.assertIsInstance(cause, TimeoutError)
        self.assertIs(cause, self.owner.retirement_failure)
        self.assertIs(cause, self.session._observation_failure)
        with self.assertRaises(TimeoutError) as joined:
            await self.owner.join_retirement(wait_seconds=1)
        self.assertIs(cause, joined.exception)
        self.assertIs(original, self.owner.reader)
        with self.assertRaises(LifecycleRejected) as retried:
            self.session.observe_retirement()
        self.assertIs(cause, retried.exception.__cause__)

    async def test_failed_greeting_preserves_setup_cause_without_starting_retirement_read(self):
        executor = self.owner._executor
        with patch.object(executor, "shutdown", wraps=executor.shutdown) as shutdown:
            opening = asyncio.create_task(self.owner.connect(wait_seconds=2))
            peer, _ = await asyncio.to_thread(self.listener.accept)
            self.addCleanup(peer.close)
            peer.sendall(b"X")
            with self.assertRaises(LifecycleRejected) as failed:
                await opening
            async with asyncio.timeout(1):
                while not shutdown.called:
                    await asyncio.sleep(0)
            shutdown.assert_called_once_with(wait=False)
        for thread in executor._threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        cause = failed.exception
        self.assertIs(cause, self.owner.setup.exception())
        self.assertIs(cause, self.owner.setup_failure)
        self.assertIs(cause, self.session._observation_failure)
        self.owner.request_stop()
        with self.assertRaises(LifecycleRejected) as joined:
            await self.owner.join_retirement(wait_seconds=1)
        self.assertIs(cause, joined.exception)
        self.assertIsNone(self.owner.reader)

    async def test_cancelled_setup_wait_keeps_original_greeting_read(self):
        opening = asyncio.create_task(self.owner.connect(wait_seconds=2))
        peer, _ = await asyncio.to_thread(self.listener.accept)
        self.addCleanup(peer.close)
        original = self.owner.setup
        opening.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await opening
        self.assertFalse(original.done())
        self.owner.request_stop()
        science, remote = socket.socketpair()
        self.addCleanup(remote.close)
        with science:
            send_greeting(peer, {"kind": "opened", "role": "fixture", "lock": [0, 0],
                                "generation": "1" * 32, "operation": "2" * 32},
                          science, deadline=time.monotonic() + 1)
        await self.owner.connect(wait_seconds=1)
        self.assertIs(original, self.owner.setup)
        self.assertIsNone(self.owner.reader)
        peer.sendall(self.retirement_frame())
        await self.owner.join_retirement(wait_seconds=1)
        self.assertIsNone(self.session.original_exit)
