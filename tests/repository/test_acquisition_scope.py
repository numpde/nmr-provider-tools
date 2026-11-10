#!/usr/bin/env python3
"""Exercise the real acquisition scope through fake engine and mapping ports."""

from tools.container_guard import require_container

require_container()

import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


class AcquisitionScopeTests(unittest.TestCase):
    def launch(self, callback, *, env=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        binary = root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        # Execute checked-in source through a symlink; scratch remains noexec.
        docker.symlink_to(Path(__file__).resolve().parent / "fixtures/acquisition_engine.sh")
        script = r'''
set -uo pipefail
if [[ ${OWNER_ERREXIT:-0} == 1 ]]; then set -e; fi
source host/acquisition_tool/owner.sh
# Replace only the public mapping port: the scope, workers, native client
# supervisor, signal handlers and engine retirement remain real.
acquisition_map_owner() { printf -v "$1" '%s' 0; printf -v "$2" '%s' 0; printf '%s\n' "$3" >> "$STATE/map-images"; }
receipt="$STATE/retired"
trap 'code=$?; if [[ -f "$receipt" ]] && cmp -s "$receipt" <(printf "retired\n"); then ready=yes; else ready=no; fi; printf "%s %s\n" "$code" "$ready" >> "$STATE/caller-exit"; if [[ ${CALLER_EXITS:-0} == 1 ]]; then exit "$code"; fi' EXIT
grants=(--read-only --entrypoint python)
application=(-c ignored)
callback() {
    CALLBACK_BODY
}
acquisition_scope --endpoint unix:///var/run/docker.sock --bootstrap-image fixture:bootstrap --retired-receipt "$receipt" -- callback
'''.replace('CALLBACK_BODY', callback)
        injected = {}
        if env is not None:
            injected.update(env)
        environment = {**os.environ, 'PATH': f'{binary}:{os.environ["PATH"]}',
                       'STATE': str(root), 'ENGINE_STATE': str(root), **injected}
        process = subprocess.Popen(['bash', '-c', script], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        def stop():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        self.addCleanup(stop)
        return process, root

    def wait_marker(self, process, marker):
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f'owner exited before {marker.name}: {process.communicate()}')
            time.sleep(.01)
        self.assertTrue(marker.exists(), msg=f'owner did not create {marker}')

    def assert_exit(self, process, root, status):
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(status, process.returncode, msg=(stdout, stderr))
        self.assertEqual([f'{status} yes'], (root / 'caller-exit').read_text().splitlines(), msg=(stdout, stderr))
        return stdout

    def test_callback_return_and_exit_preserve_status_and_retire_before_caller_exit(self):
        for action in ('return 2', 'exit 2'):
            with self.subTest(action=action):
                process, root = self.launch(action)
                self.assert_exit(process, root, 2)

    def test_failed_create_exact_pair_owns_cleanup_without_start(self):
        roles = ('gateway', 'relay', 'worker-1')
        for index, role in enumerate(roles, 1):
            with self.subTest(role=role):
                process, root = self.launch(
                    'acquisition_set_route system; '
                    'acquisition_worker online status fixture:local grants application',
                    env={'FAIL_CREATE_ROLE': role, 'FAILED_CREATE_OUTPUT': 'exact'})
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(2, process.returncode, msg=(stdout, stderr))
                self.assertEqual(['2 yes'], (root / 'caller-exit').read_text().splitlines())
                self.assertIn(f'Cannot create {role}: creation did not complete (status 23).'.encode(), stderr)
                identifiers = [f'{value:064x}' for value in range(1, index + 1)]
                self.assertEqual(
                    {f'start-{identifier}' for identifier in identifiers[:-1]},
                    {path.name for path in root.glob('start-*')})
                self.assertEqual(
                    {f'rm-{identifier}' for identifier in identifiers},
                    {path.name for path in root.glob('rm-*')})
                self.assertEqual(list(reversed(roles[:index])),
                                 (root / 'removal-attempts').read_text().splitlines())
                self.assertEqual([], list(root.glob('live-*')))
                self.assertFalse(Path((root / 'scratch').read_text().strip()).exists())
                match = re.search(r'--config (/tmp/nmrnet-docker-client\.[^/]+)/config',
                                  (root / 'engine-client').read_text())
                self.assertIsNotNone(match)
                self.assertFalse(Path(match.group(1)).exists())

    def test_failed_create_disagreeing_or_nul_stdout_retains_ownership(self):
        roles = ('gateway', 'relay', 'worker-1')
        for index, role in enumerate(roles, 1):
            for output in ('disagree', 'nul'):
                with self.subTest(role=role, output=output):
                    process, root = self.launch(
                        'acquisition_set_route system; '
                        'acquisition_worker online status fixture:local grants application',
                        env={'FAIL_CREATE_ROLE': role, 'FAILED_CREATE_OUTPUT': output})
                    stdout, stderr = process.communicate(timeout=20)
                    operation = Path((root / 'scratch').read_text().strip())
                    self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                    match = re.search(r'--config (/tmp/nmrnet-docker-client\.[^/]+)/config',
                                      (root / 'engine-client').read_text())
                    self.assertIsNotNone(match)
                    client = Path(match.group(1))
                    self.addCleanup(shutil.rmtree, client, ignore_errors=True)
                    self.assertEqual(2, process.returncode, msg=(stdout, stderr))
                    self.assertEqual(['2 no'], (root / 'caller-exit').read_text().splitlines())
                    self.assertFalse((root / 'retired').exists())
                    self.assertTrue(operation.is_dir())
                    self.assertTrue(client.is_dir())
                    self.assertIn(f'Cannot create {role}: creation did not complete (status 23).'.encode(), stderr)
                    identifiers = [f'{value:064x}' for value in range(1, index + 1)]
                    self.assertEqual(
                        {f'start-{identifier}' for identifier in identifiers[:-1]},
                        {path.name for path in root.glob('start-*')})
                    self.assertEqual([], list(root.glob('rm-*')))
                    self.assertFalse((root / 'removal-attempts').exists())
                    self.assertEqual({f'live-{identifier}' for identifier in identifiers},
                                     {path.name for path in root.glob('live-*')})
                    identifier = identifiers[-1].encode()
                    self.assertEqual(identifier + b'\n', (operation / role / 'cid').read_bytes())
                    expected = f'{9:064d}\n'.encode() if output == 'disagree' else identifier + b'\0'
                    self.assertEqual(expected, (operation / role / 'stdout').read_bytes())

    def test_term_to_parent_during_worker_retires_then_chains_caller_exit(self):
        process, root = self.launch('acquisition_worker offline status fixture:local grants application')
        self.wait_marker(process, root / 'started-worker')
        process.send_signal(signal.SIGTERM)
        self.assert_exit(process, root, 143)
        self.assertTrue((root / 'removed-worker').exists())

    def test_cancelled_create_uses_only_admissible_identity_for_cleanup(self):
        roles = ('gateway', 'relay', 'worker-1')
        for index, role in enumerate(roles, 1):
            for output in ('empty', 'exact', 'disagree', 'nul'):
                with self.subTest(role=role, output=output):
                    process, root = self.launch(
                        'acquisition_set_route system; '
                        'acquisition_worker online status fixture:local grants application',
                        env={'FAIL_CREATE_ROLE': role, 'FAILED_CREATE_OUTPUT': output,
                             'HOLD_FAILED_CREATE': '1'})
                    try:
                        self.wait_marker(process, root / 'create-held')
                        client_pid = int((root / 'create-held').read_text())
                        anchor_pid = os.getpgid(client_pid)
                        self.assertEqual(process.pid, os.getsid(anchor_pid))
                        self.assertTrue(Path('/proc', str(client_pid)).exists())
                        self.assertIsNone(process.poll())
                        operation = Path((root / 'scratch').read_text().strip())
                        self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                        match = re.search(r'--config (/tmp/nmrnet-docker-client\.[^/]+)/config',
                                          (root / 'engine-client').read_text())
                        self.assertIsNotNone(match)
                        private_client = Path(match.group(1))
                        self.addCleanup(shutil.rmtree, private_client, ignore_errors=True)
                        identifiers = [f'{value:064x}' for value in range(1, index + 1)]
                        identifier = identifiers[-1].encode()
                        cid = operation / role / 'cid'
                        create_stdout = operation / role / 'stdout'
                        self.assertEqual(identifier + b'\n', cid.read_bytes())
                        if output == 'empty':
                            # A complete EOF receipt permits recovery before stdout publication.
                            cid.write_bytes(identifier)
                            expected_cid, expected_stdout = identifier, b''
                            self.assertEqual(64, cid.stat().st_size)
                        elif output == 'exact':
                            expected_cid = expected_stdout = identifier + b'\n'
                        elif output == 'disagree':
                            expected_cid, expected_stdout = identifier + b'\n', f'{9:064d}\n'.encode()
                        else:
                            expected_cid, expected_stdout = identifier + b'\n', identifier + b'\0'
                        self.assertEqual(expected_cid, cid.read_bytes())
                        self.assertEqual(expected_stdout, create_stdout.read_bytes())
                        self.assertEqual(str(index), (root / 'counter').read_text())
                        process.send_signal(signal.SIGTERM)
                        stdout, stderr = process.communicate(timeout=15)
                        self.assertEqual(143, process.returncode, msg=(stdout, stderr))
                        self.assertEqual(b'', stdout)
                        self.assertFalse(Path('/proc', str(client_pid)).exists())
                        self.assertFalse(Path('/proc', str(anchor_pid)).exists())
                        self.assertEqual({f'start-{value}' for value in identifiers[:-1]},
                                         {path.name for path in root.glob('start-*')})
                        self.assertEqual(str(index), (root / 'counter').read_text())
                        if output in ('empty', 'exact'):
                            self.assertEqual({f'rm-{value}' for value in identifiers},
                                             {path.name for path in root.glob('rm-*')})
                            self.assertEqual(list(reversed(roles[:index])),
                                             (root / 'removal-attempts').read_text().splitlines())
                            self.assertEqual([], list(root.glob('live-*')))
                            self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
                            self.assertEqual(['143 yes'], (root / 'caller-exit').read_text().splitlines())
                            self.assertFalse(operation.exists())
                            self.assertFalse(private_client.exists())
                        else:
                            self.assertEqual([], list(root.glob('rm-*')))
                            self.assertFalse((root / 'removal-attempts').exists())
                            self.assertEqual({f'live-{value}' for value in identifiers},
                                             {path.name for path in root.glob('live-*')})
                            self.assertFalse((root / 'retired').exists())
                            self.assertEqual(['143 no'], (root / 'caller-exit').read_text().splitlines())
                            self.assertTrue(operation.is_dir())
                            self.assertTrue(private_client.is_dir())
                            self.assertEqual(expected_cid, cid.read_bytes())
                            self.assertEqual(expected_stdout, create_stdout.read_bytes())
                    finally:
                        if process.poll() is None:
                            process.send_signal(signal.SIGTERM)
                            process.communicate(timeout=35)

    def test_errexit_unwind_retires_the_session_before_caller_exit(self):
        process, root = self.launch(
            'acquisition_set_route system; acquisition_worker online status fixture:local grants application; false',
            env={'OWNER_ERREXIT': '1', 'WORKER_STATUS': '0'})
        try:
            self.assert_exit(process, root, 1)
            self.assertEqual(['worker-1', 'relay', 'gateway'],
                             (root / 'removal-attempts').read_text().splitlines())
        finally:
            # The negative control deliberately loses the old owner's locals;
            # its fake engine has no live resources beyond these exact files.
            if (root / 'scratch').exists():
                shutil.rmtree(Path((root / 'scratch').read_text().strip()), ignore_errors=True)
            if (root / 'engine-client').exists():
                match = re.search(r'--config (/tmp/nmrnet-docker-client\.[^/]+)/config',
                                  (root / 'engine-client').read_text())
                if match:
                    shutil.rmtree(Path(match.group(1)), ignore_errors=True)

    def test_worker_result_names_do_not_collide(self):
        callback = r'''
local status=91 report_status=92
local -a engine=(false)
local inference_retirement_confirmed=false inference_cancellation_status=999
acquisition_worker offline status fixture:first-worker grants application || return $?
acquisition_worker offline report_status fixture:second-worker grants application || return $?
printf '%s %s\n' "$status" "$report_status"
'''
        process, root = self.launch(callback, env={'WORKER_STATUS': '7'})
        self.assertIn(b'7 7\n', self.assert_exit(process, root, 0))
        self.assertEqual(['fixture:bootstrap'], (root / 'map-images').read_text().splitlines())

    def test_online_workers_share_one_private_resolver_capture(self):
        process, root = self.launch(
            'acquisition_set_route system; acquisition_worker online status fixture:first grants application; '
            'acquisition_worker online status fixture:second grants application',
            env={'WORKER_STATUS': '0'})
        self.assert_exit(process, root, 0)
        arguments = (root / 'create-arguments').read_text().splitlines()
        self.assertEqual(1, arguments.count('--resolver-file'))
        self.assertEqual(1, arguments.count('/gateway-resolv.conf'))
        mounts = [value for value in arguments if 'dst=/gateway-resolv.conf' in value]
        self.assertEqual(1, len(mounts))
        self.assertTrue(mounts[0].endswith('/resolv.conf,dst=/gateway-resolv.conf,readonly'))
        self.assertNotIn('/run/systemd/resolve/resolv.conf', '\n'.join(arguments))

    def test_worker_stdout_is_streamed_before_worker_finishes(self):
        process, root = self.launch(
            'acquisition_worker offline status fixture:local grants application',
            env={'STREAM_WITNESS': '1', 'WORKER_STATUS': '0'})
        self.wait_marker(process, root / 'started-worker')
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, msg='worker output was buffered until completion')
        self.assertEqual(b'streamed\n', process.stdout.readline())
        self.assertIsNone(process.poll())
        (root / 'release-worker').touch()
        self.assert_exit(process, root, 0)

    def test_full_diagnostic_pipe_does_not_prevent_caller_input_cleanup(self):
        for action in ("return 2", "exit 2"):
            with self.subTest(action=action):
                callback = """
python3 - <<'PY'
import os
os.set_blocking(2, False)
try:
    while True:
        os.write(2, b'x' * 4096)
except BlockingIOError:
    pass
finally:
    os.set_blocking(2, True)
PY
_acq_fail 'fixture diagnostic'
""" + action
                process, root = self.launch(callback, env={'CALLER_EXITS': '1'})
                # Do not drain stderr until the caller has observed retirement.
                self.wait_marker(process, root / 'caller-exit')
                self.assert_exit(process, root, 2)

    def test_exiting_caller_trap_preserves_the_framework_diagnostic(self):
        process, root = self.launch("_acq_fail 'known framework diagnostic'; exit 2",
                                    env={'CALLER_EXITS': '1'})
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(2, process.returncode, msg=(stdout, stderr))
        self.assertIn(b'known framework diagnostic', stderr)
        self.assertEqual(['2 yes'], (root / 'caller-exit').read_text().splitlines())

    def test_failed_worker_or_relay_removal_retains_dependencies_and_receipt(self):
        for failed, forbidden in (('worker-1', ('relay', 'gateway')), ('relay', ('gateway',))):
            with self.subTest(failed=failed):
                process, root = self.launch(
                    'acquisition_set_route system; acquisition_worker online status fixture:local grants application',
                    env={'WORKER_STATUS': '0', 'FAIL_REMOVE': failed})
                stdout, stderr = process.communicate(timeout=20)
                # This fake-engine test deliberately creates unresolved owner
                # evidence; remove only its observed test operation artifacts.
                operation = Path((root / 'scratch').read_text().strip())
                self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                match = re.search(r'--config (/tmp/nmrnet-docker-client\.[^/]+)/config',
                                  (root / 'engine-client').read_text())
                if match:
                    self.addCleanup(shutil.rmtree, Path(match.group(1)), ignore_errors=True)
                self.assertNotEqual(process.returncode, 0, msg=(stdout, stderr))
                self.assertFalse((root / 'retired').exists(), msg=(stdout, stderr, list(root.iterdir())))
                self.assertEqual([f'{process.returncode} no'], (root / 'caller-exit').read_text().splitlines())
                attempts = (root / 'removal-attempts').read_text().splitlines()
                self.assertIn(failed, attempts)
                for role in forbidden:
                    self.assertNotIn(role, attempts)


if __name__ == "__main__":
    unittest.main()
