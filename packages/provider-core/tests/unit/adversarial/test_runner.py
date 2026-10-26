"""A session cannot inherit another operation's retirement authority."""

import os
import array
import fcntl
import signal
import socket
import threading
import time
import unittest

from provider_core.runner_protocol import LifecycleRejected, receive_document, receive_greeting
from provider_core.runner_session import RunnerSession
from tests.fakes.runner import RunningSupervisor, worker_pid


class RunnerRefusalTests(unittest.TestCase):
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
