"""Resource obligations and the order in which failures reach the caller."""
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock, patch

from helpers import Engine, Guard, OBSERVATION, request
from linux_idmap._docker import DockerError
from linux_idmap._errors import IdentityError
from linux_idmap._files import FileIdentity
from linux_idmap._procfs import ReaderEvidence
from linux_idmap._probe_model import ProbeFailure, ResourceState, StepError
from linux_idmap.probe import probe_identity


class LifecycleOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.guard = Engine(), Guard()
        self.enterContext(patch('linux_idmap._probe_scope.DockerEngine.connect', return_value=self.engine))
        self.enterContext(patch('linux_idmap._probe_scope.ProbeProcesses', return_value=self.guard))

    def failure(self, cancel=None):
        with self.assertRaises(ProbeFailure) as raised:
            probe_identity(request(), cancel=cancel)
        self.assertNotIn('uid_map', raised.exception.as_dict())
        self.assertNotIn('gid_map', raised.exception.as_dict())
        return raised.exception

    def test_failure_order_is_execution_order_not_diagnostic_bucket_order(self):
        scenarios = [
            # measurement, removal, confirmation, close, late cancellation
            (False, True, False, False, False, ['engine_changed'], ResourceState.RETIRED),
            (False, True, True, True, False, ['engine_changed', 'confirmation', 'local-io'], ResourceState.UNRESOLVED),
            (False, False, False, True, False, ['local-io'], ResourceState.RETIRED),
            (False, True, False, False, True, ['engine_changed', 'cancelled'], ResourceState.RETIRED),
            (False, False, False, False, True, ['cancelled'], ResourceState.RETIRED),
            (True, True, True, True, True, ['measurement', 'engine_changed', 'confirmation', 'local-io', 'cancelled'], ResourceState.UNRESOLVED),
        ]
        for measure, remove, confirm, close, cancel_late, expected, state in scenarios:
            with self.subTest(expected=expected):
                self.engine, self.guard = Engine(), Guard()
                cancellation = threading.Event()
                measurement = StepError('measurement', 'measurement failed')
                removal = DockerError('engine_changed', 'engine continuity failed during removal', mutation_uncertain=True)
                confirmation = IdentityError('confirmation', 'retirement confirmation failed')
                closing = OSError('PRIVATE close details')
                if measure:
                    self.guard.reader_evidence = Mock(side_effect=['host-evidence', measurement])
                def remove_container(*args, **kwargs):
                    self.assertIsNone(kwargs.get('cancel'))
                    self.engine.container = None  # Lost removal reply, not a surviving resource.
                    if remove:
                        raise removal
                self.engine.remove = Mock(side_effect=remove_container)
                if confirm:
                    self.guard.process_retired = Mock(side_effect=confirmation)
                def close_client():
                    if cancel_late:
                        cancellation.set()
                    if close:
                        raise closing
                self.engine.close = Mock(side_effect=close_client)
                with patch('linux_idmap._probe_scope.DockerEngine.connect', return_value=self.engine), \
                     patch('linux_idmap._probe_scope.ProbeProcesses', return_value=self.guard):
                    failure = self.failure(cancellation)
                actual = [failure.problem.category, *(p.category for p in failure.retirement.problems)]
                self.assertEqual(expected, actual)
                self.assertEqual(state, failure.retirement.state)
                first = measurement if measure else removal if remove else closing if close else None
                if first is not None:
                    self.assertIs(first, failure.__cause__)
                self.assertNotIn('PRIVATE', str(failure.as_dict()))

    def restart_before_retirement(self):
        inspections = 0
        def inspect(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 4:
                engine.container = replace(engine.container, pid=43, started_at='restarted')
        self.engine.on_inspect = inspect

    def test_restart_requires_both_initial_and_current_process_to_retire(self):
        self.restart_before_retirement()
        current = replace(OBSERVATION.identity, pid=43, starttime=456)
        self.guard.identify = Mock(return_value=current)
        self.guard.observe = Mock(wraps=self.guard.observe)
        self.guard.process_retired = Mock(return_value=True)
        failure = self.failure()
        self.assertEqual('process-identity', failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        identities = [call.args[0] for call in self.guard.process_retired.call_args_list]
        self.assertEqual([OBSERVATION.identity, current], identities)
        self.assertEqual(2, self.guard.observe.call_count)

    def test_same_reported_run_cannot_hide_a_replaced_process_identity(self):
        current = replace(OBSERVATION.identity, starttime=456)
        self.guard.identify = Mock(return_value=current)
        self.guard.process_retired = Mock(return_value=True)
        failure = self.failure()
        self.assertEqual('process-identity', failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual([OBSERVATION.identity, current],
                         [call.args[0] for call in self.guard.process_retired.call_args_list])
        self.assertIn('remove', self.engine.events)
        self.assertIsNone(self.engine.container)

    def test_changed_reader_evidence_after_measurement_blocks_map_delivery(self):
        first = ReaderEvidence(4026531837, FileIdentity(1, 2), 123, 123)
        changed = replace(first, procfs=FileIdentity(1, 3))
        self.guard.reader_evidence = Mock(side_effect=[first, changed])
        failure = self.failure()
        self.assertEqual('host-precondition', failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual(2, self.guard.reader_evidence.call_count)
        self.assertIn('remove', self.engine.events)
        self.assertIsNone(self.engine.container)

    def test_created_container_disappearing_before_start_is_not_a_measurement(self):
        self.engine.on_inspect = lambda engine: setattr(engine, 'container', None)
        self.guard.observe = Mock(wraps=self.guard.observe)
        failure = self.failure()
        self.assertEqual('process-identity', failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.guard.observe.assert_not_called()
        self.assertNotIn('start', self.engine.events)
        self.assertNotIn('remove', self.engine.events)
        self.assertEqual(2, self.engine.events.count('inspect'))
        self.assertIn('engine-check', self.engine.events)

    def test_either_surviving_process_blocks_retirement_after_restart(self):
        self.restart_before_retirement()
        current = replace(OBSERVATION.identity, pid=43, starttime=456)
        self.guard.identify = Mock(return_value=current)
        self.guard.process_retired = Mock(side_effect=lambda identity: identity != current)
        self.assertEqual(ResourceState.UNRESOLVED, self.failure().retirement.state)
        self.assertIn(OBSERVATION.identity,
                      [call.args[0] for call in self.guard.process_retired.call_args_list])

    def test_original_process_cannot_be_forgotten_after_restart(self):
        self.restart_before_retirement()
        current = replace(OBSERVATION.identity, pid=43, starttime=456)
        self.guard.identify = Mock(return_value=current)
        self.guard.process_retired = Mock(side_effect=lambda identity: identity != OBSERVATION.identity)
        self.assertEqual(ResourceState.UNRESOLVED, self.failure().retirement.state)

    def test_unidentified_current_pid_does_not_replace_initial_identity_obligation(self):
        self.restart_before_retirement()
        self.guard.identify = Mock(side_effect=IdentityError('identify-current', 'current identity unavailable'))
        self.guard.process_retired = Mock(return_value=True)
        self.guard.pid_absent = Mock(return_value=True)
        failure = self.failure()
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.guard.process_retired.assert_called_once_with(OBSERVATION.identity)
        self.guard.pid_absent.assert_called_once_with(43)
        self.assertEqual('process-identity', failure.problem.category)
        self.assertEqual(['identify-current'], [problem.category for problem in failure.retirement.problems])

    def test_unidentified_replacement_still_present_blocks_retirement(self):
        self.restart_before_retirement()
        self.guard.identify = Mock(side_effect=IdentityError('identify-current', 'current identity unavailable'))
        self.guard.process_retired = Mock(return_value=True)
        self.guard.pid_absent = Mock(return_value=False)
        self.assertEqual(ResourceState.UNRESOLVED, self.failure().retirement.state)
        self.guard.process_retired.assert_called_once_with(OBSERVATION.identity)

    def test_changed_ownership_after_acquisition_never_authorizes_removal(self):
        inspections = 0
        def change_owner(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 4:
                engine.container = replace(engine.container, owner='foreign')
        self.engine.on_inspect = change_owner
        failure = self.failure()
        self.assertEqual('ownership', failure.problem.category)
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertNotIn('remove', self.engine.events)

    def test_engine_change_during_final_confirmation_prevents_delivery(self):
        self.engine.assert_unchanged = Mock(side_effect=DockerError('engine_changed', 'engine changed'))
        failure = self.failure()
        self.assertEqual('engine_changed', failure.problem.category)
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertIsNone(self.engine.container)

    def test_stopped_flag_with_positive_pid_is_not_normal_exit(self):
        inspections = 0
        def contradict(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 4:
                engine.container = replace(engine.container, running=False)
        self.engine.on_inspect = contradict
        self.guard.identify = Mock(wraps=self.guard.identify)
        failure = self.failure()
        self.assertEqual('process-identity', failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual(42, self.guard.identify.call_args.args[0])

    def test_running_pid_zero_is_not_evidence_of_absence(self):
        inspections = 0
        def contradict(engine):
            nonlocal inspections
            inspections += 1
            if inspections == 4:
                engine.container = replace(engine.container, running=True, pid=0)
        self.engine.on_inspect = contradict
        self.guard.identify = Mock(side_effect=IdentityError('process-identity', 'invalid PID'))
        self.guard.pid_absent = Mock(side_effect=IdentityError('process-identity', 'invalid PID cannot prove absence'))
        self.guard.process_retired = Mock(return_value=True)
        failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertEqual('process-identity', failure.problem.category)
        self.guard.pid_absent.assert_called_once_with(0)
        self.guard.process_retired.assert_called_once_with(OBSERVATION.identity)

    def test_remove_reply_failure_is_settled_only_by_confirmed_retirement(self):
        for category in ('timeout', 'docker_command'):
            with self.subTest(category=category):
                self.engine.on_remove = lambda engine: setattr(engine, 'container', None)
                self.engine.remove_failure = DockerError(category, 'remove reply lost', mutation_uncertain=True)
                result = probe_identity(request())
                self.assertEqual(OBSERVATION.uid_map, result.uid_map)
                self.assertIsNone(self.engine.container)

    def test_reconciled_remove_reply_does_not_hide_cancellation_or_close_failure(self):
        self.engine.on_remove = lambda engine: setattr(engine, 'container', None)
        self.engine.remove_failure = DockerError('timeout', 'remove reply lost', mutation_uncertain=True)
        cancellation = threading.Event()
        self.engine.close = cancellation.set
        failure = self.failure(cancellation)
        self.assertEqual('cancelled', failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.engine.close = Mock(side_effect=OSError('PRIVATE close details'))
        failure = self.failure()
        self.assertEqual('local-io', failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)

    def test_retirement_never_erases_safety_interrupt_or_client_cleanup_failures(self):
        errors = (
            DockerError('engine_changed', 'engine changed', mutation_uncertain=True),
            ExceptionGroup('command and cleanup failed', [DockerError('timeout', 'reply lost'),
                           DockerError('client_cleanup', 'client retirement unconfirmed')]),
            KeyboardInterrupt(), ValueError('PRIVATE unexpected failure'),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.engine.on_remove = lambda engine: setattr(engine, 'container', None)
                self.engine.remove_failure = error
                failure = self.failure()
                self.assertIs(error.exceptions[0] if isinstance(error, BaseExceptionGroup) else error, failure.__cause__)
                self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
                self.assertNotIn('PRIVATE', str(failure.as_dict()))

    def test_nested_native_groups_keep_order_and_redact_each_leaf(self):
        first = ValueError('PRIVATE operation detail')
        self.engine.on_remove = lambda engine: setattr(engine, 'container', None)
        self.engine.remove_failure = BaseExceptionGroup('PRIVATE group detail', [
            first, BaseExceptionGroup('PRIVATE cleanup detail', [
                KeyboardInterrupt(), DockerError('client_cleanup', 'client retirement unconfirmed')])])
        cancellation = threading.Event()
        self.engine.close = cancellation.set
        failure = self.failure(cancellation)
        self.assertIs(first, failure.__cause__)
        self.assertEqual('internal', failure.problem.category)
        self.assertEqual(['cancelled', 'client_cleanup'], [p.category for p in failure.retirement.problems])
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertNotIn('PRIVATE', str(failure.as_dict()))

    def test_normal_exit_after_observation_needs_retirement_not_live_resampling(self):
        for auto_removed in (False, True):
            with self.subTest(auto_removed=auto_removed):
                engine, guard = Engine(), Guard()
                inspections = 0
                def exit_probe(engine):
                    nonlocal inspections
                    inspections += 1
                    if inspections == 4:
                        engine.container = None if auto_removed else replace(engine.container, running=False, pid=0)
                engine.on_inspect = exit_probe
                guard.observe = Mock(wraps=guard.observe)
                guard.identify = Mock(wraps=guard.identify)
                guard.process_retired = Mock(return_value=True)
                with patch('linux_idmap._probe_scope.DockerEngine.connect', return_value=engine), \
                     patch('linux_idmap._probe_scope.ProbeProcesses', return_value=guard):
                    result = probe_identity(request())
                self.assertEqual(OBSERVATION.uid_map, result.uid_map)
                self.assertEqual(OBSERVATION.gid_map, result.gid_map)
                self.assertEqual(2, guard.observe.call_count)
                guard.identify.assert_not_called()
                guard.process_retired.assert_called_once_with(OBSERVATION.identity)
                self.assertIsNone(engine.container)


if __name__ == '__main__':
    unittest.main()
