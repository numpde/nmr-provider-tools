"""Operation completion and process retirement remain independently observable."""

import os
import json
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
