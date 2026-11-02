"""Every observed identity remains a retirement obligation after a bad sample."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
from helpers import Engine, Guard, OBSERVATION, request
from linux_idmap.probe import probe_identity, ProbeFailure
from linux_idmap._probe_model import ResourceState
from linux_idmap._errors import IdentityError

class FailedAcquisitionObligations(unittest.TestCase):
    def test_second_observed_process_is_not_forgotten_when_container_disappears(self):
        engine, guard = Engine(), Guard()
        replacement = replace(OBSERVATION, identity=replace(OBSERVATION.identity, starttime=124))
        guard.observe = Mock(side_effect=[OBSERVATION, replacement])
        guard.process_retired = Mock(side_effect=lambda identity: identity != replacement.identity)
        inspections = 0
        def disappear(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 4:
                engine.container = None
        engine.on_inspect = disappear
        with patch('linux_idmap.probe.DockerEngine.connect', return_value=engine), \
             patch('linux_idmap.probe.ProbeProcesses', return_value=guard):
            with self.assertRaises(ProbeFailure) as raised:
                probe_identity(request())
        self.assertEqual(ResourceState.UNRESOLVED, raised.exception.retirement.state)
        identities = [call.args[0] for call in guard.process_retired.call_args_list]
        self.assertIn(OBSERVATION.identity, identities)
        self.assertIn(replacement.identity, identities)
        self.assertNotIn('uid_map', raised.exception.as_dict())

    def test_failed_first_observation_keeps_reported_pid_obligation(self):
        engine, guard = Engine(), Guard()
        guard.observe = Mock(side_effect=IdentityError('process-identity', 'maps unavailable'))
        guard.pid_absent = Mock(return_value=False)
        inspections = 0
        def disappear(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 3:
                engine.container = None
        engine.on_inspect = disappear
        with patch('linux_idmap.probe.DockerEngine.connect', return_value=engine), \
             patch('linux_idmap.probe.ProbeProcesses', return_value=guard):
            with self.assertRaises(ProbeFailure) as raised:
                probe_identity(request())
        self.assertEqual(ResourceState.UNRESOLVED, raised.exception.retirement.state)
        self.assertTrue(guard.pid_absent.called)
        self.assertEqual(42, guard.pid_absent.call_args.args[0])

    def test_changed_second_run_keeps_its_unobserved_pid_obligation(self):
        engine, guard = Engine(), Guard()
        guard.observe = Mock(wraps=guard.observe)
        guard.pid_absent = Mock(return_value=False)
        inspections = 0
        def replace_then_disappear(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 3:
                engine.container = replace(engine.container, pid=43, started_at='replacement')
            if inspections == 4:
                engine.container = None
        engine.on_inspect = replace_then_disappear
        with patch('linux_idmap.probe.DockerEngine.connect', return_value=engine), \
             patch('linux_idmap.probe.ProbeProcesses', return_value=guard):
            with self.assertRaises(ProbeFailure) as raised:
                probe_identity(request())
        self.assertEqual(ResourceState.UNRESOLVED, raised.exception.retirement.state)
        self.assertEqual(1, guard.observe.call_count)
        self.assertTrue(guard.pid_absent.called)
        self.assertEqual(43, guard.pid_absent.call_args.args[0])

    def test_later_live_identity_resolves_earlier_reports_at_the_same_pid(self):
        engine, guard = Engine(), Guard()
        current = replace(OBSERVATION.identity, starttime=456)
        guard.observe = Mock(side_effect=IdentityError('process-identity', 'maps unavailable'))
        guard.identify = Mock(return_value=current)
        guard.process_retired = Mock(return_value=True)
        guard.pid_absent = Mock(return_value=False)  # Exited process still has a zombie PID.
        inspections = 0
        def replace_run(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 3:
                engine.container = replace(engine.container, started_at='replacement')
        engine.on_inspect = replace_run
        with patch('linux_idmap.probe.DockerEngine.connect', return_value=engine), \
             patch('linux_idmap.probe.ProbeProcesses', return_value=guard):
            with self.assertRaises(ProbeFailure) as raised:
                probe_identity(request())
        self.assertEqual(ResourceState.RETIRED, raised.exception.retirement.state)
        guard.process_retired.assert_called_once_with(current)
        guard.pid_absent.assert_not_called()

    def test_three_observed_identities_remain_ordered_retirement_obligations(self):
        engine, guard = Engine(), Guard()
        second = replace(OBSERVATION, identity=replace(OBSERVATION.identity, starttime=124))
        third = replace(OBSERVATION.identity, pid=43, starttime=456)
        guard.observe = Mock(side_effect=[OBSERVATION, second])
        guard.identify = Mock(return_value=third)
        guard.process_retired = Mock(return_value=True)
        inspections = 0
        def replacement(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 4:
                engine.container = replace(engine.container, pid=43, started_at='replacement')
        engine.on_inspect = replacement
        with patch('linux_idmap.probe.DockerEngine.connect', return_value=engine), \
             patch('linux_idmap.probe.ProbeProcesses', return_value=guard):
            with self.assertRaises(ProbeFailure) as raised:
                probe_identity(request())
        self.assertEqual(ResourceState.RETIRED, raised.exception.retirement.state)
        self.assertEqual([OBSERVATION.identity, second.identity, third],
                         [call.args[0] for call in guard.process_retired.call_args_list])

if __name__ == '__main__':
    unittest.main()
