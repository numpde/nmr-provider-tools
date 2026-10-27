"""A session cannot inherit another operation's retirement authority."""

import os
import array
from concurrent.futures import ThreadPoolExecutor
import fcntl
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from provider_core.runner import serve
from provider_core.runner_protocol import LifecycleRejected, receive_document, receive_greeting, send_document, send_greeting
from provider_core.runner_session import RunnerSession
from tests.fakes.runner import RunningSupervisor, worker_pid


class RunnerRefusalTests(unittest.TestCase):
    def test_a_second_retirement_reader_cannot_consume_the_original_frame(self):
        runner = RunningSupervisor()
        self.addCleanup(runner.close)
        session = runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(session.close)
        session.connect()
        pid = worker_pid(session.science, b"H")
        entered, release = threading.Event(), threading.Event()
        receive = session._receive_retirement

        def original_read(deadline):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("Original lifecycle read was not released by its test owner")
            return receive(deadline)

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(session, "_receive_retirement", side_effect=original_read):
            original = pool.submit(session.observe_retirement)
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(LifecycleRejected):
                    session.observe_retirement()
            finally:
                os.kill(pid, signal.SIGKILL)
                release.set()
            original.result(timeout=3)
        self.assertEqual(-9, session.original_exit.returncode)

    def test_legacy_retirement_has_unknown_reason_and_invented_reason_is_refused(self):
        for reason in (None, "oom", [], 1):
            with self.subTest(reason=reason):
                session = RunnerSession(
                    "unused", role="fixture", expected_lock_identity=(0, 0),
                    startup_seconds=1, retirement_seconds=1,
                )
                self.addCleanup(session.close)
                session.control.close()
                sender, session.control = socket.socketpair()
                with sender:
                    session.generation, session.operation = "generation", "operation"
                    reply = {
                        "kind": "retired", "generation": "generation", "operation": "operation",
                        "returncode": -9, "scratch_released": True,
                    }
                    if reason is not None:
                        reply["reason"] = reason
                    send_document(sender, reply, deadline=time.monotonic() + 1)
                    if reason is None:
                        session.observe_retirement()
                        self.assertIsNone(session.retirement.reason)
                    else:
                        with self.assertRaises(LifecycleRejected):
                            session.observe_retirement()
                        self.assertIsNone(session.retirement)

    def test_status_directory_is_private_and_canonical_before_any_status_write(self):
        for condition, failure_type in (("mode", PermissionError), ("symlink", ValueError)):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                endpoint, scratch, status = (root / name for name in ("endpoint", "scratch", "status"))
                for directory in (endpoint, scratch, status):
                    directory.mkdir(mode=0o700)
                (endpoint / ".owner-session.lock").touch(mode=0o600)
                selected = status
                if condition == "mode":
                    status.chmod(0o777)
                else:
                    selected = root / "status-alias"
                    selected.symlink_to(status, target_is_directory=True)
                options = SimpleNamespace(socket=endpoint / "runner.sock", scratch=scratch,
                                          status_directory=selected, role="fixture")
                read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
                try:
                    os.write(write_fd, b"x")
                    with self.assertRaises(failure_type):
                        serve(options, [sys.executable, "-c", "raise SystemExit(77)"], read_fd)
                    self.assertEqual([], list(status.iterdir()))
                    self.assertFalse(options.socket.exists())
                finally:
                    os.close(read_fd)
                    os.close(write_fd)

    def test_cli_requires_exactly_one_explicit_operation_lifetime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for lifetime in ([], ["--operation-seconds", "1", "--owner-session-lifetime"]):
                with self.subTest(lifetime=lifetime):
                    result = subprocess.run([
                        sys.executable, "-m", "provider_core.runner", "--role", "fixture",
                        "--socket", str(root / "absent" / "runner.sock"), "--scratch", str(root / "scratch"),
                        "--status-directory", str(root / "status"), "--startup-seconds", "1",
                        "--terminate-seconds", "0.1", "--kill-seconds", "1", "--cleanup-seconds", "1",
                        *lifetime, "--", sys.executable, "-c", "raise SystemExit(77)",
                    ], capture_output=True, timeout=2)
                    self.assertEqual(2, result.returncode)
                    self.assertEqual([], list(root.iterdir()))

    def test_failed_greeting_cannot_admit_a_retirement_with_null_operation_identity(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket(socket.AF_UNIX) as listener:
            path = str(Path(temporary) / "runner.sock")
            listener.bind(path)
            listener.listen()
            listener.settimeout(1)
            session = RunnerSession(
                path, role="fixture", expected_lock_identity=(0, 0), startup_seconds=1, retirement_seconds=1,
            )
            self.addCleanup(session.close)
            science, remote = socket.socketpair()
            with science, remote:
                def reply():
                    peer, _ = listener.accept()
                    with peer:
                        send_greeting(peer, {"kind": "malformed"}, science, deadline=time.monotonic() + 1)
                        send_document(peer, {
                            "kind": "retired", "generation": None, "operation": None,
                            "returncode": 0, "scratch_released": True,
                        }, deadline=time.monotonic() + 1)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    original = executor.submit(reply)
                    with self.assertRaises(LifecycleRejected) as setup:
                        session.connect()
                    original.result(timeout=2)
                science.close()
                remote.settimeout(1)
                self.assertEqual(b"", remote.recv(1))
                with self.assertRaises(LifecycleRejected) as observation:
                    session.observe_retirement()
                self.assertIs(setup.exception, observation.exception.__cause__)
                self.assertIsNone(session.retirement)

    def test_partial_greeting_timeout_closes_its_descriptor_and_blocks_another_frame_read(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket(socket.AF_UNIX) as listener:
            path = str(Path(temporary) / "runner.sock")
            listener.bind(path)
            listener.listen()
            listener.settimeout(1)
            session = RunnerSession(
                path, role="fixture", expected_lock_identity=(0, 0), startup_seconds=0.2, retirement_seconds=1,
            )
            self.addCleanup(session.close)
            with ThreadPoolExecutor(max_workers=1) as executor:
                original = executor.submit(session.connect)
                peer, _ = listener.accept()
                science, remote = socket.socketpair()
                with peer, science, remote:
                    peer.sendmsg([b"\0\0\0"], [
                        (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [science.fileno()])),
                    ])
                    science.close()
                    with self.assertRaises(TimeoutError) as setup:
                        original.result(timeout=1)
                    self.assertIs(TimeoutError, type(setup.exception))
                    self.assertTrue(session.control_connected)
                    self.assertIsNone(session.operation)
                    remote.settimeout(1)
                    self.assertEqual(b"", remote.recv(1))
                    retired = {
                        "kind": "retired", "generation": None, "operation": None,
                        "returncode": 0, "scratch_released": True,
                    }
                    send_document(peer, retired, deadline=time.monotonic() + 1)
                    with self.assertRaises(LifecycleRejected) as observation:
                        session.observe_retirement()
                    self.assertIs(setup.exception, observation.exception.__cause__)
                    self.assertIsNone(session.retirement)
                    self.assertEqual(retired, receive_document(session.control, deadline=time.monotonic() + 1))

    def test_extra_received_descriptors_are_closed_on_rejection(self):
        sender, receiver = socket.socketpair()
        one, two = socket.socketpair()
        with sender, receiver, one, two:
            before = len(os.listdir("/proc/self/fd"))
            sender.sendmsg([b"\0"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [one.fileno(), two.fileno()]))])
            with self.assertRaises(LifecycleRejected):
                receive_greeting(receiver, deadline=time.monotonic() + 1)
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_partial_control_delivery_cannot_reset_the_absolute_phase_deadline(self):
        sender, receiver = socket.socketpair()
        with sender, receiver:
            def drip():
                try:
                    for byte in b"\0\0\0\x02{}":
                        sender.sendall(bytes([byte]))
                        time.sleep(0.04)
                # silent-defaults: allow-contract -- The deadline fixture may close its peer before the drip thread settles.
                except OSError:
                    pass
            thread = threading.Thread(target=drip)
            thread.start()
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    receive_document(receiver, deadline=started + 0.09)
            finally:
                receiver.close()
                thread.join(timeout=1)
            self.assertFalse(thread.is_alive())

    def test_child_holds_lock_after_supervisor_death_until_child_exit(self):
        runner = RunningSupervisor()
        self.addCleanup(runner.close)
        session = runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(session.close)
        session.connect()
        pid = worker_pid(session.science, b"H")
        self.addCleanup(self._kill_if_live, pid)
        runner.process.kill()
        runner.process.wait(timeout=2)
        with (runner.endpoint / ".owner-session.lock").open("rb") as contender:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail("Child exit did not release the inherited lifetime lease")
                    time.sleep(0.01)
        runner.restart_with_fresh_scratch()
        successor = runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(successor.close)
        successor.connect()
        self.assertEqual(successor.lock_identity, session.lock_identity)
        self.assertNotEqual(successor.generation, session.generation)
        worker_pid(successor.science)
        successor.observe_retirement()

    def test_wrong_role_or_replaced_lock_is_refused_before_scientific_handoff(self):
        for role, inode_delta in (("foreign", 0), ("fixture", 1)):
            with self.subTest(role=role, inode_delta=inode_delta):
                runner = RunningSupervisor()
                self.addCleanup(runner.close)
                observed = (runner.endpoint / ".owner-session.lock").stat()
                session = RunnerSession(
                    runner.path, role=role, expected_lock_identity=(observed.st_dev, observed.st_ino + inode_delta),
                    startup_seconds=2, retirement_seconds=2,
                )
                self.addCleanup(session.close)
                with self.assertRaises(LifecycleRejected):
                    session.connect()
                self.assertIsNone(session.science)

    def test_received_cleanup_failure_keeps_reaped_child_facts_and_stops_admission(self):
        runner = RunningSupervisor()
        self.addCleanup(runner.close)
        session = runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(session.close)
        session.connect()
        worker_pid(session.science, b"C")
        with self.assertRaises(LifecycleRejected):
            session.observe_retirement()
        self.assertEqual(session.retirement.returncode, 0)
        self.assertFalse(session.retirement.scratch_released)
        self.assertEqual("exited", session.retirement.reason)
        with self.assertRaises(LifecycleRejected):
            session.retire()
        self.assertNotEqual(runner.process.wait(timeout=2), 0)

    @staticmethod
    def _kill_if_live(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        # silent-defaults: allow-contract -- A cleanup kill racing an already-dead fixture process is complete.
        except ProcessLookupError:
            pass

    def test_peer_without_transferred_scientific_descriptor_is_rejected(self):
        sender, receiver = socket.socketpair()
        with sender, receiver:
            sender.sendall(b"x")
            with self.assertRaises(LifecycleRejected):
                receive_greeting(receiver, deadline=time.monotonic() + 1)

    def test_absolute_operation_deadline_stops_connected_but_idle_owner(self):
        runner = RunningSupervisor(operation_seconds=0.25)
        self.addCleanup(runner.close)
        session = runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(session.close)
        session.connect()
        pid = worker_pid(session.science, b"H")
        session.observe_retirement()
        self.assertEqual(session.retirement.returncode, -9)
        self.assertEqual("deadline", session.retirement.reason)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_busy_runner_does_not_spawn_second_child_without_retirement(self):
        runner = RunningSupervisor()
        self.addCleanup(runner.close)
        first = runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(first.close)
        first.connect()
        worker_pid(first.science, b"H")
        second = runner.session(startup_seconds=0.05, retirement_seconds=2)
        self.addCleanup(second.close)
        with self.assertRaises(TimeoutError):
            second.connect()
        second.close()
        first.retire()
        third = runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(third.close)
        third.connect()
        self.assertEqual(third.generation, first.generation)
        worker_pid(third.science)
        third.observe_retirement()
