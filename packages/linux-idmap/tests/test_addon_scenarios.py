"""Scenarios for checked observations and managed probes."""

from dataclasses import replace
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import CID, Engine, Guard, request
from linux_idmap import MapRows, observe_process, read_maps
from linux_idmap import _procfs as host
from linux_idmap._docker import DockerError
from linux_idmap._errors import IdentityError
from linux_idmap.process import observe_host_process
from linux_idmap.probe import ProbeFailure, probe_identity
from linux_idmap._probe_spec import OWNER


class SourceRefinementScenarios(unittest.TestCase):
    def test_existing_tokenless_process_needs_neither_docker_nor_host_qualification(self):
        child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', 'import time; time.sleep(30)'])
        try:
            sample = observe_process(child.pid)
            raw = MapRows(list(sample.uid_rows), list(sample.gid_rows))
            with host.coherent_process(child.pid) as source:
                self.assertEqual(raw, host.consistent_maps(source.read))
            if os.stat('/proc/self/ns/user').st_ino != 4026531837:
                with self.assertRaises(IdentityError) as caught:
                    observe_host_process(child.pid)
                self.assertEqual('host-precondition', caught.exception.category)
            else:
                self.assertEqual(raw.uid_rows, observe_host_process(child.pid).uid_map.rows)
        finally:
            child.terminate()
            child.wait(timeout=3)

    def test_repeated_source_rejects_change_that_single_sample_cannot_detect(self):
        def changing_source():
            readings = iter((b'0 1000 1\n', b'0 2000 1\n'))
            return lambda name: next(readings) if name == 'uid_map' else b'0 3000 1\n'
        self.assertEqual(1000, next(read_maps(changing_source()).uid_rows).outside)
        with self.assertRaises(IdentityError):
            host.consistent_maps(changing_source())

    def test_process_exit_is_detected_before_qualified_scope_returns(self):
        child = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(30)'])
        try:
            with self.assertRaises(IdentityError):
                with host.coherent_process(child.pid) as source:
                    self.assertTrue(list(read_maps(source.read).uid_rows))
                    child.terminate()
                    child.wait(timeout=3)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)


class ManagedProbeScenarios(unittest.TestCase):
    def run_probe(self, policy, engine, guard=None):
        with patch('linux_idmap._probe_scope.DockerEngine.connect', return_value=engine), \
             patch('linux_idmap._probe_scope.ProbeProcesses', return_value=guard or Guard()):
            return probe_identity(policy)

    def test_image_before_deployment_supplies_maps_without_an_existing_application(self):
        engine = Engine()
        self.assertIsNone(engine.container)
        result = self.run_probe(request(), engine)
        self.assertEqual(101000, result.uid_map.translate(1000))
        self.assertLess(engine.events.index('create'), engine.events.index('start'))
        self.assertIn('remove', engine.events)
        self.assertIsNone(engine.container)

    def test_default_probe_creates_and_verifies_confinement(self):
        engine = Engine()
        self.assertEqual(101000, self.run_probe(request(), engine).uid_map.translate(1000))
        for flag, value in (('--network', 'none'), ('--cap-drop', 'ALL'),
                            ('--security-opt', 'no-new-privileges:true')):
            self.assertEqual(value, engine.arguments[engine.arguments.index(flag) + 1])
        self.assertIn('--read-only', engine.arguments)

        changed = {'NetworkMode': 'host', 'ReadonlyRootfs': False, 'CapDrop': [],
                   'SecurityOpt': [], 'Privileged': True, 'PidMode': 'host',
                   'CapAdd': ['SYS_ADMIN'], 'Devices': [{'PathOnHost': '/dev/sda'}],
                   'DeviceRequests': [{'Count': -1}]}
        for field, value in changed.items():
            with self.subTest(field=field):
                engine = Engine()
                engine.on_create = lambda current: current.container.isolation.update({field: value})
                with self.assertRaises(ProbeFailure) as caught:
                    self.run_probe(request(), engine)
                self.assertEqual('confinement', caught.exception.problem.category)
                self.assertNotIn('start', engine.events)
                self.assertNotIn('uid_map', caught.exception.as_dict())

    def test_default_probe_refuses_mounts_before_start(self):
        engine = Engine()
        def add_mount(current):
            current.container = replace(current.container, mounts=[{'Destination': '/data'}])
        engine.on_create = add_mount
        with self.assertRaises(ProbeFailure) as caught:
            self.run_probe(request(), engine)
        self.assertEqual('confinement', caught.exception.problem.category)
        self.assertNotIn('start', engine.events)
        self.assertNotIn('uid_map', caught.exception.as_dict())

    def test_lost_reply_reference_finds_owned_resource_without_adopting_foreign_one(self):
        engine = Engine()
        engine.create_failure = DockerError('timeout', 'lost reply', mutation_uncertain=True)
        engine.remove_failure = DockerError('timeout', 'lost removal reply', mutation_uncertain=True)
        with self.assertRaises(ProbeFailure) as caught:
            self.run_probe(request(), engine)
        report = json.loads(json.dumps(caught.exception.as_dict()))
        reference = report['handle']
        # An operator uses only the emitted coordinates for read-only inspection.
        endpoints = {engine.identity.endpoint: engine}
        selected = endpoints[reference['endpoint']]
        self.assertEqual(reference['daemon_id'], selected.identity.info.daemon_id)
        foreign = replace(engine.container, cid='f' * 64, token='f' * 32)
        inventory = [foreign, engine.container]
        owned = [item for item in inventory if item.token == reference['operation_token']
                 and item.owner == OWNER and item.cid == reference['container_id']]
        self.assertEqual([CID], [item.cid for item in owned])
        self.assertNotIn('uid_map', report)
        self.assertEqual(1, engine.events.count('create'))

    def test_invalid_acquisition_sample_does_not_skip_verified_cleanup(self):
        for malformed in (b'not integers\n', b'0 1\n', b'0 1 2 3\n', b'0 1000 1\n1 1001\n'):
            with self.subTest(malformed=malformed):
                engine, guard = Engine(), Guard()
                observations = []
                def fail_during_acquisition(_guard):
                    observations.append(True)
                    if len(observations) == 2:
                        host.consistent_maps(lambda name: malformed)
                guard.on_observe = fail_during_acquisition
                with self.assertRaises(ProbeFailure) as caught:
                    self.run_probe(request(), engine, guard)
                self.assertEqual('process-identity', caught.exception.problem.category)
                self.assertIn('remove', engine.events)
                self.assertIsNone(engine.container)

    def test_previous_owner_protocol_cannot_authorize_adoption(self):
        engine = Engine()
        engine.on_create = lambda item: setattr(item, 'container', replace(item.container, owner='identity-v2'))
        with self.assertRaises(ProbeFailure) as caught:
            self.run_probe(request(), engine)
        self.assertEqual('ownership', caught.exception.problem.category)
        self.assertNotIn('start', engine.events)
        self.assertNotIn('remove', engine.events)
