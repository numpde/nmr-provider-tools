"""The observable contract: fresh maps or an honest failure, never unproved success."""

from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import Mock, patch

from linux_idmap.addons.probe import ProbeFailure, ResourceState, probe_identity
from linux_idmap.addons.docker import DockerError
from linux_idmap.core import IdRange
from linux_idmap import IdMap
from linux_idmap.addons.errors import IdentityError
from linux_idmap.addons.model import StepError
from helpers import CID, Engine, Guard, OBSERVATION, request


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.request = request()
        self.engine, self.guard = Engine(), Guard()
        for target, value in (("DockerEngine.connect", self.engine), ("ProbeProcesses", self.guard)):
            self.enterContext(patch("linux_idmap.addons.probe." + target, return_value=value))

    def failure(self, **kwargs):
        with self.assertRaises(ProbeFailure) as caught:
            probe_identity(self.request, **kwargs)
        self.assertNotIn("uid_map", caught.exception.as_dict())
        self.assertNotIn("gid_map", caught.exception.as_dict())
        return caught.exception

    def test_success_releases_full_maps_only_after_exact_retirement(self):
        result = probe_identity(self.request)
        self.assertEqual(101000, result.uid_map.translate(1000))
        self.assertEqual(202000, result.gid_map.translate(2000))
        self.assertEqual("1000:2000", self.engine.arguments[self.engine.arguments.index("--user") + 1])
        self.assertIsNone(self.engine.container)
        self.assertEqual(["remove"], [x for x in self.engine.events if x == "remove"])

    def test_lost_creation_reply_reconciles_without_second_create(self):
        self.engine.create_failure = DockerError("timeout", "Docker reply timed out", mutation_uncertain=True)
        failure = self.failure()
        self.assertEqual("timeout", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertIn("candidates", self.engine.events)
        self.assertEqual(1, self.engine.events.count("create"))

    def test_empty_inventory_does_not_settle_lost_creation(self):
        self.engine.create_failure = DockerError("timeout", "Docker reply timed out", mutation_uncertain=True)
        self.engine.hide_creation = True
        failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertNotIn("remove", self.engine.events)

    def test_wrong_token_never_authorizes_removal(self):
        self.engine.on_create = lambda engine: setattr(engine, "container", replace(engine.container, token="foreign"))
        failure = self.failure()
        self.assertEqual("ownership", failure.problem.category)
        self.assertNotIn("remove", self.engine.events)
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)

    def test_changed_isolation_never_authorizes_removal(self):
        def weaken(engine):
            engine.container.isolation["Privileged"] = True
        self.engine.on_create = weaken
        self.assertEqual("confinement", self.failure().problem.category)
        self.assertNotIn("remove", self.engine.events)

    def test_remove_failure_cannot_release_mapping(self):
        self.engine.remove_failure = DockerError("docker_command", "removal did not report success", mutation_uncertain=True)
        failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertEqual("docker_command", failure.problem.category)
        self.assertIs(self.engine.remove_failure, failure.__cause__)

    def test_cancellation_after_create_cleans_without_starting(self):
        cancel = threading.Event()
        self.engine.on_create = lambda engine: cancel.set()
        failure = self.failure(cancel=cancel)
        self.assertEqual("cancelled", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertNotIn("start", self.engine.events)

    def test_cancellation_before_dispatch_does_not_create(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(ResourceState.NOT_CREATED, self.failure(cancel=cancel).retirement.state)
        self.assertNotIn("create", self.engine.events)

    def test_cancellation_during_retirement_does_not_interrupt_cleanup(self):
        cancel = threading.Event()
        self.engine.on_remove = lambda engine: cancel.set()
        failure = self.failure(cancel=cancel)
        self.assertEqual("cancelled", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)

    def test_changed_acquisition_identity_still_allows_cleanup(self):
        calls = 0
        def change(guard):
            nonlocal calls
            calls += 1
            if calls == 2:
                guard.observation = replace(OBSERVATION, identity=replace(OBSERVATION.identity, starttime=124))
        self.guard.on_observe = change
        failure = self.failure()
        self.assertEqual("process-identity", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual((), failure.retirement.problems)

    def test_changed_container_start_time_rejects_evidence_before_rereading_maps(self):
        for inspection_number in (3,):
            self.engine, self.guard = Engine(), Guard()
            inspections = 0
            def change_start(engine):
                nonlocal inspections
                inspections += 1
                if inspections == inspection_number:
                    engine.container = replace(engine.container, started_at="restarted")
            self.engine.on_inspect = change_start
            self.guard.observe = Mock(wraps=self.guard.observe)
            with self.subTest(inspection_number=inspection_number):
                with patch("linux_idmap.addons.probe.DockerEngine.connect", return_value=self.engine), \
                     patch("linux_idmap.addons.probe.ProbeProcesses", return_value=self.guard):
                    failure = self.failure()
                self.assertEqual("process-identity", failure.problem.category)
                self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
                self.assertEqual(inspection_number - 2, self.guard.observe.call_count)

    def test_changed_maps_during_observation_are_rejected(self):
        calls = 0
        def change(guard):
            nonlocal calls
            calls += 1
            if calls == 2:
                guard.observation = replace(OBSERVATION, uid_map=IdMap((IdRange(0, 300000, 65536),)))
        self.guard.on_observe = change
        self.assertEqual("process-identity", self.failure().problem.category)

    def test_exited_probe_is_removed_but_not_successful(self):
        self.engine.on_start = lambda engine: setattr(engine, "container", replace(engine.container, running=False))
        self.assertEqual("process-identity", self.failure().problem.category)
        self.assertIn("remove", self.engine.events)

    def test_nonrunning_flag_cannot_hide_a_live_reported_pid(self):
        self.engine.on_start = lambda engine: setattr(engine, "container", replace(engine.container, running=False))
        self.guard.retired = False
        failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertIn("remove", self.engine.events)

    def test_live_process_prevents_retirement_claim(self):
        self.guard.retired = False
        self.assertEqual(ResourceState.UNRESOLVED, self.failure().retirement.state)

    def test_proven_predispatch_failure_is_not_created(self):
        self.engine.create_failure = DockerError("engine_changed", "engine changed before dispatch")
        self.engine.hide_creation = True
        self.assertEqual(ResourceState.NOT_CREATED, self.failure().retirement.state)
        self.assertNotIn("candidates", self.engine.events)

    def test_foreign_group_without_dispatch_metadata_cannot_prove_noncreation(self):
        original = ValueError('PRIVATE command failure')
        self.engine.create_failure = ExceptionGroup('PRIVATE group detail', [original])
        failure = self.failure()
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertIn('candidates', self.engine.events)
        self.assertIn('remove', self.engine.events)
        self.assertIs(original, failure.__cause__)
        self.assertNotIn('PRIVATE', str(failure.as_dict()))

    def test_owned_predispatch_group_keeps_known_noncreation(self):
        from linux_idmap.addons.docker import _command_failure
        self.engine.hide_creation = True
        self.engine.create_failure = _command_failure(
            DockerError('timeout', 'engine guard timed out'),
            DockerError('client_cleanup', 'guard client retirement unconfirmed'), mutation_uncertain=False)
        failure = self.failure()
        self.assertEqual(ResourceState.NOT_CREATED, failure.retirement.state)
        self.assertNotIn('candidates', self.engine.events)
        self.assertEqual(['client_cleanup'], [problem.category for problem in failure.retirement.problems])

    def test_policy_rejected_before_engine_contact(self):
        for changes in ({"endpoint": "ssh://remote"}, {"interpreter": "python"},
                        {"user_uid": True}, {"user_gid": 4294967295}):
            with self.subTest(changes=changes), self.assertRaises(StepError) as caught:
                replace(request(), **changes)
            self.assertEqual("invalid-policy", caught.exception.category)
            self.assertEqual([], self.engine.events)

    def test_safe_reason_is_preserved_and_unexpected_exception_text_is_redacted(self):
        self.engine.create_failure = DockerError("timeout", "Docker reply timed out", mutation_uncertain=True)
        self.assertEqual("Docker reply timed out", self.failure().problem.message)
        self.engine.create_failure = ValueError("SECRET CONTENT")
        self.assertNotIn("SECRET", json.dumps(self.failure().as_dict()))

    def test_python_traceback_keeps_internal_cause_without_serializing_it(self):
        original = ValueError("SECRET DEBUG DETAIL")
        self.engine.create_failure = original
        failure = self.failure()
        self.assertIs(original, failure.__cause__)
        self.assertIsNotNone(failure.__cause__.__traceback__)
        self.assertEqual("internal", failure.problem.category)
        self.assertNotIn("SECRET", json.dumps(failure.as_dict()))

    def test_interrupted_cleanup_entry_never_claims_no_creation(self):
        with patch("linux_idmap.addons.probe._Probe.retire", side_effect=KeyboardInterrupt):
            failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertEqual(CID, failure.retirement.container_id)
        self.assertEqual("cancelled", failure.problem.category)
        self.assertIsNotNone(self.engine.container)

    def test_cancellation_during_client_close_withholds_mapping(self):
        cancel = threading.Event()
        self.engine.close = cancel.set
        failure = self.failure(cancel=cancel)
        self.assertEqual("cancelled", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)

    def test_original_failure_and_secondary_cleanup_problems_are_both_retained(self):
        self.engine.create_failure = DockerError("timeout", "creation reply was lost", mutation_uncertain=True)
        self.engine.remove_failure = DockerError("docker_command", "removal failed", mutation_uncertain=True)
        failure = self.failure()
        self.assertEqual("timeout", failure.problem.category)
        self.assertIs(self.engine.create_failure, failure.__cause__)
        self.assertEqual("creation reply was lost", failure.problem.message)
        self.assertTrue(any(problem.category == "docker_command" for problem in failure.retirement.problems))

    def test_retirement_failure_precedes_later_client_close_failure(self):
        removal = DockerError("docker_command", "removal failed", mutation_uncertain=True)
        closing = OSError("private close detail")
        self.engine.remove_failure = removal
        self.engine.close = Mock(side_effect=closing)
        failure = self.failure()
        self.assertEqual("docker_command", failure.problem.category)
        self.assertIs(removal, failure.__cause__)
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertEqual(["timeout", "local-io"], [p.category for p in failure.retirement.problems])
        self.assertNotIn("private close detail", str(failure.as_dict()))

    def test_measurement_failure_precedes_close_failure(self):
        creation = DockerError("timeout", "creation reply lost", mutation_uncertain=True)
        self.engine.create_failure = creation
        self.engine.close = Mock(side_effect=OSError("private close detail"))
        failure = self.failure()
        self.assertEqual("timeout", failure.problem.category)
        self.assertIs(creation, failure.__cause__)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual(["local-io"], [p.category for p in failure.retirement.problems])

    def test_unidentified_pid_can_retire_without_erasing_identity_failure(self):
        self.engine.on_start = lambda engine: setattr(engine, "container", replace(engine.container, running=False))
        self.guard.identify = Mock(side_effect=IdentityError("process-identity", "identity unavailable"))
        self.guard.pid_absent = Mock(return_value=True)
        failure = self.failure()
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.guard.pid_absent.assert_called_once_with(42)
        self.assertEqual(["process-identity"], [p.category for p in failure.retirement.problems])
        self.assertIn("remove", self.engine.events)

    def test_unidentified_pid_still_present_prevents_retirement(self):
        self.engine.on_start = lambda engine: setattr(engine, "container", replace(engine.container, running=False))
        self.guard.identify = Mock(side_effect=IdentityError("process-identity", "identity unavailable"))
        self.guard.pid_absent = Mock(return_value=False)
        failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertIsNone(self.engine.container)
        self.assertEqual(["process-identity", "timeout"], [p.category for p in failure.retirement.problems])

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
            self.assertTrue(method.called, msg=name)
            for call in method.call_args_list:
                self.assertIsNone(call.kwargs["cancel"])
                self.assertGreater(call.kwargs["timeout"], 0)
                self.assertLessEqual(call.kwargs["timeout"], request().bounds.command_seconds)


if __name__ == "__main__":
    unittest.main()
