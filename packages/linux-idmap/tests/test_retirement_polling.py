"""A repeated same-run inspect does not undo exact process retirement evidence."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
from helpers import Engine, Guard, OBSERVATION, request
from linux_idmap._docker import DockerError
from linux_idmap.probe import probe_identity
from linux_idmap._probe_model import ProbeFailure, ResourceState

class RetirementPolling(unittest.TestCase):
    def test_same_run_visible_during_removal_can_retire_as_zombie(self):
        engine, guard = Engine(), Guard()
        engine.remove_failure = DockerError('timeout', 'remove reply lost', mutation_uncertain=True)
        inspections = 0
        def eventual_absence(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 6:  # First post-remove query still sees the same run.
                engine.container = None
        engine.on_inspect = eventual_absence
        guard.process_retired = Mock(return_value=True)  # Exact process is exited/zombie.
        guard.pid_absent = Mock(return_value=False)  # Numeric PID still exists until reap.
        with patch('linux_idmap._probe_scope.DockerEngine.connect', return_value=engine), \
             patch('linux_idmap._probe_scope.ProbeProcesses', return_value=guard):
            result = probe_identity(request())
        self.assertEqual(OBSERVATION.uid_map, result.uid_map)
        guard.pid_absent.assert_not_called()
        guard.process_retired.assert_called_once_with(OBSERVATION.identity)

    def test_new_run_during_removal_remains_an_obligation_and_a_failure(self):
        for replacement_pid, absent in ((42, False), (42, True), (43, False), (43, True)):
            with self.subTest(pid=replacement_pid, replacement_pid_absent=absent):
                engine, guard = Engine(), Guard()
                engine.remove_failure = DockerError('timeout', 'remove reply lost', mutation_uncertain=True)
                inspections = 0
                def replacement_then_absence(engine):
                    nonlocal inspections
                    inspections += 1
                    if inspections == 5:
                        engine.container = replace(engine.container, pid=replacement_pid, started_at='replacement')
                    if inspections == 6:
                        engine.container = None
                engine.on_inspect = replacement_then_absence
                guard.process_retired = Mock(return_value=True)
                guard.pid_absent = Mock(return_value=absent)
                with patch('linux_idmap._probe_scope.DockerEngine.connect', return_value=engine), \
                     patch('linux_idmap._probe_scope.ProbeProcesses', return_value=guard):
                    with self.assertRaises(ProbeFailure) as caught:
                        probe_identity(request())
                expected = ResourceState.RETIRED if absent else ResourceState.UNRESOLVED
                self.assertEqual(expected, caught.exception.retirement.state)
                categories = [caught.exception.problem.category,
                              *(problem.category for problem in caught.exception.retirement.problems)]
                expected_categories = ['process-identity'] if absent else ['timeout', 'process-identity', 'timeout']
                self.assertEqual(expected_categories, categories)
                self.assertEqual(replacement_pid, guard.pid_absent.call_args.args[0])
                guard.process_retired.assert_called_once_with(OBSERVATION.identity)
                self.assertNotIn('uid_map', caught.exception.as_dict())

if __name__ == '__main__':
    unittest.main()
