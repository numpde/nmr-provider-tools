"""Characterize failure ordering independently from successful resource retirement."""

from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock, patch

from helpers import CID, Engine, Guard, request
from provider_deploy import ProbeFailure, ResourceState, probe_identity
from provider_deploy.docker import DockerError
from provider_deploy.linux import LinuxError


class OutcomePrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.guard = Engine(), Guard()
        for name, instance in (("DockerEngine.connect", self.engine), ("LinuxGuard", self.guard)):
            mocked = patch("provider_deploy.operation." + name, return_value=instance)
            mocked.start()
            self.addCleanup(mocked.stop)

    def failure(self, **kwargs):
        with self.assertRaises(ProbeFailure) as caught:
            probe_identity(request(), **kwargs)
        failure = caught.exception
        self.assertNotIn("uid_map", failure.as_dict())
        self.assertNotIn("gid_map", failure.as_dict())
        return failure

    def test_close_failure_precedes_retirement_problems_without_measurement_failure(self):
        removal = DockerError("docker_command", "removal failed", mutation_uncertain=True)
        closing = OSError("private close detail")
        self.engine.remove_failure = removal
        self.engine.close = Mock(side_effect=closing)
        failure = self.failure()
        self.assertEqual("close", failure.problem.stage)
        self.assertIs(closing, failure.__cause__)
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertEqual(["remove", "retire"], [p.stage for p in failure.retirement.problems])
        self.assertNotIn("private close detail", str(failure.as_dict()))

    def test_measurement_failure_precedes_close_failure(self):
        creation = DockerError("timeout", "creation reply lost", mutation_uncertain=True)
        self.engine.create_failure = creation
        self.engine.close = Mock(side_effect=OSError("private close detail"))
        failure = self.failure()
        self.assertEqual("create", failure.problem.stage)
        self.assertIs(creation, failure.__cause__)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual(["close"], [p.stage for p in failure.retirement.problems])

    def test_unidentified_pid_can_retire_without_erasing_identity_failure(self):
        self.engine.on_start = lambda engine: setattr(engine, "container", replace(engine.container, running=False))
        self.guard.identify = Mock(side_effect=LinuxError("process-identity", "identity unavailable"))
        self.guard.pid_absent = Mock(return_value=True)
        failure = self.failure()
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual((42,), failure.handle.unobserved_pids)
        self.assertEqual((), failure.handle.processes)
        self.guard.pid_absent.assert_called_once_with(42)
        self.assertEqual(["observe"], [p.stage for p in failure.retirement.problems])
        self.assertIn("remove", self.engine.events)

    def test_unidentified_pid_still_present_prevents_retirement(self):
        self.engine.on_start = lambda engine: setattr(engine, "container", replace(engine.container, running=False))
        self.guard.identify = Mock(side_effect=LinuxError("process-identity", "identity unavailable"))
        self.guard.pid_absent = Mock(return_value=False)
        failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertEqual((42,), failure.handle.unobserved_pids)
        self.assertIsNone(self.engine.container)
        self.assertEqual(["observe", "retire"], [p.stage for p in failure.retirement.problems])

    def test_multiple_candidates_never_authorize_removal(self):
        self.engine.create_failure = DockerError("timeout", "creation reply lost", mutation_uncertain=True)
        self.engine.candidates = Mock(return_value=(CID, "d" * 64))
        failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertIsNone(failure.handle.container_id)
        self.assertEqual("reconciliation", failure.retirement.problems[0].category)
        self.assertNotIn("remove", self.engine.events)
        self.assertEqual(1, self.engine.events.count("create"))

    def test_cleanup_commands_do_not_receive_measurement_cancellation(self):
        cancellation = threading.Event()
        self.engine.on_create = lambda engine: cancellation.set()
        methods = {}
        for name in ("inspect", "remove", "assert_unchanged"):
            methods[name] = Mock(wraps=getattr(self.engine, name))
            setattr(self.engine, name, methods[name])
        failure = self.failure(cancel=cancellation)
        self.assertEqual("cancelled", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        for name, method in methods.items():
            self.assertTrue(method.called, name)
            for call in method.call_args_list:
                self.assertIsNone(call.kwargs["cancel"])
                self.assertGreater(call.kwargs["timeout"], 0)
                self.assertLessEqual(call.kwargs["timeout"], request().bounds.command_seconds)


if __name__ == "__main__":
    unittest.main()
