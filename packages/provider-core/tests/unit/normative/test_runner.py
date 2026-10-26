"""Operation completion and process retirement remain independently observable."""

import os
import json
import time
import unittest

from tests.fakes.runner import RunningSupervisor, worker_pid
from provider_core.failure_projection import owned_details
from provider_core.runner_protocol import LifecycleRejected


class RunnerTests(unittest.TestCase):
    def test_protected_failure_projection_keeps_stopped_child_and_unreleased_scratch_facts(self):
        failure = LifecycleRejected("Child stopped; scratch remains held", returncode=-9, scratch_released=False)
        self.assertEqual(owned_details(failure), {
            "message": "Child stopped; scratch remains held", "returncode": -9, "scratch_released": False,
        })
    def setUp(self):
        self.runner = RunningSupervisor()
        self.addCleanup(self.runner.close)

    def session(self):
        session = self.runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(session.close)
        session.connect()
        return session

    def test_two_operations_use_distinct_children_and_preserve_supervisor_and_lock(self):
        first = self.session()
        one = worker_pid(first.science)
        first.observe_retirement()
        self.assertEqual(first.retirement.returncode, 0)
        self.assertTrue(first.retirement.scratch_released)
        second = self.session()
        two = worker_pid(second.science)
        second.observe_retirement()
        self.assertNotEqual(one, two)
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(first.lock_identity, second.lock_identity)
        self.assertNotEqual(first.operation, second.operation)
        self.assertIsNone(self.runner.process.poll())
        self.assertEqual(list(self.runner.scratch.iterdir()), [])
        for pid in (one, two):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_native_frame_failure_does_not_corrupt_control_retirement(self):
        session = self.session()
        worker_pid(session.science, b"F")
        self.assertEqual(session.science.recv(16), b"\x00\x00")
        session.observe_retirement()
        self.assertEqual(session.retirement.returncode, 9)
        self.assertTrue(session.retirement.scratch_released)

    def test_owner_retirement_kills_term_ignoring_child_before_reply(self):
        session = self.session()
        pid = worker_pid(session.science, b"H")
        session.retire()
        self.assertEqual(session.retirement.returncode, -9)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(list(self.runner.scratch.iterdir()), [])

    def test_child_inherits_only_scientific_socket_and_the_admitted_lease(self):
        session = self.session()
        worker_pid(session.science, b"D")
        raw = bytearray()
        while not raw.endswith(b"\n"):
            part = session.science.recv(4096)
            if not part:
                self.fail("Child closed before its descriptor observation")
            raw.extend(part)
        observed = json.loads(raw)
        self.assertEqual(observed["sockets"], [observed["science"]])
        self.assertEqual(tuple(observed["lock"]), session.lock_identity)
        session.observe_retirement()


class OwnerSessionLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.runner = RunningSupervisor(operation_seconds=None)
        self.addCleanup(self.runner.close)
        self.session = self.runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(self.session.close)
        self.session.connect()

    def test_same_loaded_child_serves_repeated_requests_until_explicit_retirement(self):
        pid = worker_pid(self.session.science, b"R")
        operation = self.session.operation
        time.sleep(0.3)
        self.assertEqual(pid, worker_pid(self.session.science))
        self.assertEqual(operation, self.session.operation)
        self.assertIsNone(self.session.retirement)
        self.session.retire()
        self.assertEqual(-15, self.session.retirement.returncode)
        self.assertTrue(self.session.retirement.scratch_released)
        self.assertEqual([], list(self.runner.scratch.iterdir()))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIsNone(self.runner.process.poll())

    def test_native_child_exit_retires_its_owner_session_without_a_stop_request(self):
        pid = worker_pid(self.session.science)
        self.session.observe_retirement()
        self.assertEqual(0, self.session.retirement.returncode)
        self.assertTrue(self.session.retirement.scratch_released)
        self.assertEqual([], list(self.runner.scratch.iterdir()))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_control_hangup_reaps_child_before_successor_while_science_is_still_open(self):
        pid = worker_pid(self.session.science, b"H")
        self.session.control.close()
        successor = self.runner.session(startup_seconds=2, retirement_seconds=2)
        self.addCleanup(successor.close)
        successor.connect()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse((self.runner.scratch / self.session.operation).exists())
        self.assertNotEqual(self.session.operation, successor.operation)
        self.assertGreaterEqual(self.session.science.fileno(), 0)
        worker_pid(successor.science)
        successor.observe_retirement()
        self.assertEqual([], list(self.runner.scratch.iterdir()))

    def test_supervisor_shutdown_retires_a_term_ignoring_child_and_releases_scratch(self):
        pid = worker_pid(self.session.science, b"H")
        self.runner.process.terminate()
        self.session.observe_retirement()
        self.assertEqual(-9, self.session.retirement.returncode)
        self.assertTrue(self.session.retirement.scratch_released)
        self.assertEqual(0, self.runner.process.wait(timeout=2))
        self.assertEqual([], list(self.runner.scratch.iterdir()))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
