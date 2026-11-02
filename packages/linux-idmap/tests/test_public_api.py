"""Operation imports expose their request, result and failure types."""

from pathlib import Path
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import linux_idmap
from linux_idmap import IdMap, parse_map, process, probe
from helpers import Engine, Guard


class PublicApiTests(unittest.TestCase):
    def test_raw_rows_can_be_explicitly_translated(self):
        rows = parse_map(b"0 1000 2\n")
        self.assertEqual(1001, IdMap(rows).translate(1))

    def test_existing_process_with_controlled_host_coordinate_evidence(self):
        # Only initial-namespace qualification is substituted; procfs reads are real.
        namespace = os.stat('/proc/self/ns/user').st_ino
        with patch('linux_idmap._procfs._USER_INIT_INO', namespace):
            observed = process.observe_host_process(os.getpid())
        self.assertIsInstance(observed, process.ProcessObservation)
        self.assertEqual(os.getpid(), observed.identity.pid)
        self.assertEqual(list(parse_map(Path('/proc/self/uid_map').read_bytes())), observed.uid_map.rows)

    def test_local_image_result_and_operation_error_use_public_types(self):
        policy = probe.ProbeRequest('unix:///test.sock', 'approved:local',
                                    '/usr/bin/python3', 1000, 2000)
        engine = Engine()
        with patch('linux_idmap._probe_scope.DockerEngine.connect', return_value=engine), \
             patch('linux_idmap._probe_scope.ProbeProcesses', return_value=Guard()):
            result = probe.probe_identity(policy)
        self.assertIsInstance(result, probe.ProbeResult)
        self.assertEqual(101000, result.uid_map.translate(1000))
        self.assertIsNone(engine.container)
        with patch('linux_idmap._probe_scope.DockerEngine.connect', side_effect=OSError), \
             self.assertRaises(probe.ProbeFailure) as caught:
            probe.probe_identity(policy)
        self.assertEqual(probe.ResourceState.NOT_CREATED, caught.exception.retirement.state)

    def test_process_types_and_errors_are_available_together(self):
        self.assertEqual(
            {'observe_host_process', 'ProcessObservation', 'ProcessIdentity', 'IdentityError'},
            set(process.__all__),
        )
        self.assertTrue(all(hasattr(process, name) for name in process.__all__))
        with self.assertRaises(process.IdentityError):
            process.observe_host_process(0)

    def test_probe_policy_and_failure_types_are_available_together(self):
        self.assertEqual(
            {'probe_identity', 'ProbeRequest', 'Bounds', 'ProbeResult', 'StepError',
             'ProbeFailure', 'ResourceState', 'Problem', 'Retirement', 'ProbeHandle'},
            set(probe.__all__),
        )
        self.assertTrue(all(hasattr(probe, name) for name in probe.__all__))
        with self.assertRaises(probe.StepError):
            probe.Bounds(command_seconds=0)
        failure = probe.ProbeFailure(
            probe.Problem('cancelled', 'cancelled'),
            probe.Retirement(probe.ResourceState.NOT_CREATED),
        )
        self.assertEqual('known-not-created', failure.as_dict()['state'])

    def test_process_import_leaves_probe_and_transport_unloaded(self):
        program = '''
import sys
sys.path.insert(0, sys.argv[1])
import linux_idmap.process
assert {name for name in sys.modules if name.startswith('linux_idmap.')} == {
    'linux_idmap.core', 'linux_idmap.process', 'linux_idmap._procfs',
    'linux_idmap._errors', 'linux_idmap._files',
}
'''
        result = subprocess.run(
            [sys.executable, '-I', '-S', '-B', '-c', program,
             str(Path(linux_idmap.__file__).parent.parent)],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(0, result.returncode, result.stderr)
