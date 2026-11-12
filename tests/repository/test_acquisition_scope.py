#!/usr/bin/env python3
"""Exercise the real acquisition scope through fake engine and mapping ports."""

from tools.container_guard import require_container

require_container()

import json
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
    def run_mapping(self, body):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        binary = root / 'bin'
        binary.mkdir()
        actor = Path(__file__).resolve().parent / 'fixtures/acquisition_engine.sh'
        (binary / 'python3').symlink_to(actor)
        script = r'''
set -euo pipefail
unset _acq_active _acq_endpoint NMR_SELECTED_DOCKER_ENDPOINT
source "$ACQUISITION_SOURCE_ROOT/acquisition_tool/owner.sh"
''' + body
        environment = {
            **os.environ, 'PATH': f'{binary}:{os.environ["PATH"]}', 'ENGINE_STATE': str(root),
            'ACQUISITION_SOURCE_ROOT': str(Path(__file__).resolve().parents[2] / 'host'),
        }
        process = subprocess.Popen(['bash', '-c', script], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        def stop():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        self.addCleanup(stop)
        stdout, stderr = process.communicate(timeout=20)
        return process.returncode, stdout, stderr, root

    def test_mapping_uses_native_or_explicit_host_owner_pair(self):
        status, stdout, stderr, root = self.run_mapping(r'''
NMR_SELECTED_DOCKER_ENDPOINT=unix:///selected.sock
uid=unchanged gid=unchanged
acquisition_map_owner uid gid fixture:identity
printf '%s %s\n' "$uid" "$gid"
acquisition_map_owner uid gid fixture:identity 1007 2009
printf '%s %s\n' "$uid" "$gid"
''')
        self.assertEqual(0, status, msg=stderr)
        self.assertEqual(b'17 19\n17 19\n', stdout)
        self.assertEqual([
            'unix:///selected.sock', 'fixture:identity', str(os.geteuid()), str(os.getegid()),
            'unix:///selected.sock', 'fixture:identity', '1007', '2009',
        ], (root / 'identity-arguments').read_text().splitlines())

    def test_invalid_mapping_arguments_do_not_probe_or_replace_outputs(self):
        for arguments in ('', 'uid', 'uid gid', 'uid gid fixture:image 1007',
                          'uid gid fixture:image 1007 2009 extra',
                          'uid gid fixture:image "" 2009', 'uid gid fixture:image 1007 ""'):
            with self.subTest(arguments=arguments):
                status, stdout, stderr, root = self.run_mapping(r'''
NMR_SELECTED_DOCKER_ENDPOINT=unix:///selected.sock
uid=unchanged gid=unchanged
status=0
acquisition_map_owner ''' + arguments + r''' || status=$?
printf '%s %s %s\n' "$status" "$uid" "$gid"
''')
                self.assertEqual(0, status, msg=stderr)
                self.assertEqual(b'2 unchanged unchanged\n', stdout)
                self.assertFalse((root / 'identity-arguments').exists())
                self.assertNotIn(b'unbound variable', stderr)

    def test_mapping_uses_active_scope_endpoint_and_current_standalone_selection(self):
        status, stdout, stderr, root = self.run_mapping(r'''
NMR_SELECTED_DOCKER_ENDPOINT=unix:///standalone-a.sock
mapped() { acquisition_map_owner uid gid fixture:identity 1007 2009; }
acquisition_scope --endpoint unix:///scope.sock --bootstrap-image fixture:bootstrap -- mapped
[[ $_acq_active == false && $_acq_endpoint == unix:///scope.sock ]]
NMR_SELECTED_DOCKER_ENDPOINT=unix:///standalone-b.sock
acquisition_map_owner uid gid fixture:identity 1007 2009
printf '%s %s\n' "$uid" "$gid"
''')
        self.assertEqual(0, status, msg=stderr)
        self.assertEqual(b'17 19\n', stdout)
        self.assertEqual([
            'unix:///scope.sock', 'fixture:identity', '1007', '2009',
            'unix:///standalone-b.sock', 'fixture:identity', '1007', '2009',
        ], (root / 'identity-arguments').read_text().splitlines())

    def test_missing_active_endpoint_never_falls_back_to_standalone_selection(self):
        for mutation in ('_acq_endpoint=', 'unset _acq_endpoint'):
            with self.subTest(mutation=mutation):
                status, stdout, stderr, root = self.run_mapping(r'''
NMR_SELECTED_DOCKER_ENDPOINT=unix:///standalone.sock
mapped() { ''' + mutation + r'''; acquisition_map_owner uid gid fixture:identity 1007 2009; }
acquisition_scope --endpoint unix:///scope.sock --bootstrap-image fixture:bootstrap -- mapped
''')
                self.assertEqual(2, status, msg=stderr)
                self.assertEqual(b'', stdout)
                self.assertFalse((root / 'identity-arguments').exists())

    def test_missing_standalone_selection_does_not_reuse_stale_scope_endpoint(self):
        for selection in ('unset NMR_SELECTED_DOCKER_ENDPOINT', 'NMR_SELECTED_DOCKER_ENDPOINT='):
            with self.subTest(selection=selection):
                status, stdout, stderr, root = self.run_mapping(r'''
_acq_endpoint=unix:///stale.sock
''' + selection + r'''
acquisition_map_owner uid gid fixture:identity 1007 2009
''')
                self.assertEqual(2, status, msg=stderr)
                self.assertEqual(b'', stdout)
                self.assertFalse((root / 'identity-arguments').exists())

    def test_inactive_scope_operations_and_invalid_state_cannot_dispatch(self):
        for state in ('unset _acq_active', '_acq_active=false', '_acq_active=invalid'):
            with self.subTest(state=state):
                status, stdout, stderr, root = self.run_mapping(state + r'''
grants=(--read-only) application=(-c ignored)
route_status=0 worker_status=0
acquisition_set_route system || route_status=$?
acquisition_worker offline result fixture:image grants application || worker_status=$?
printf '%s %s\n' "$route_status" "$worker_status"
''')
                self.assertEqual(0, status, msg=stderr)
                self.assertEqual(b'2 2\n', stdout)
                self.assertFalse((root / 'identity-arguments').exists())

    def test_invalid_scope_state_is_never_treated_as_inactive(self):
        status, stdout, stderr, root = self.run_mapping(r'''
_acq_active=invalid
_acq_endpoint=unix:///unchanged.sock
NMR_SELECTED_DOCKER_ENDPOINT=unix:///selected.sock
uid=unchanged gid=unchanged
map_status=0 scope_status=0
acquisition_map_owner uid gid fixture:identity 1007 2009 || map_status=$?
acquisition_scope --endpoint unix:///new.sock --bootstrap-image fixture:bootstrap -- : || scope_status=$?
printf '%s %s %s %s\n' "$map_status" "$scope_status" "$uid" "$gid"
[[ $_acq_active == invalid && $_acq_endpoint == unix:///unchanged.sock ]]
''')
        self.assertEqual(0, status, msg=stderr)
        self.assertEqual(b'2 2 unchanged unchanged\n', stdout)
        self.assertFalse((root / 'identity-arguments').exists())

    def test_missing_worker_arguments_fail_before_mapping(self):
        for arguments in ('', '--timeout', '--timeout 30'):
            with self.subTest(arguments=arguments):
                status, stdout, stderr, root = self.run_mapping(r'''
worker() { acquisition_worker ''' + arguments + r'''; }
acquisition_scope --endpoint unix:///scope.sock --bootstrap-image fixture:bootstrap -- worker
''')
                self.assertEqual(2, status, msg=stderr)
                self.assertEqual(b'', stdout)
                self.assertNotIn(b'unbound variable', stderr)
                self.assertFalse((root / 'identity-arguments').exists())

    def test_nested_scope_refusal_preserves_the_outer_cleanup_owner(self):
        status, stdout, stderr, root = self.run_mapping(r'''
outer() {
    local endpoint=$_acq_endpoint scratch=$_acq_scratch owner=$_acq_owner_pid
    local saved_traps status=0
    saved_traps=$(trap -p EXIT HUP INT TERM)
    acquisition_scope --endpoint unix:///nested.sock --bootstrap-image fixture:nested -- : || status=$?
    [[ $status == 2 && $_acq_endpoint == "$endpoint" && $_acq_scratch == "$scratch" ]] || return 97
    [[ $_acq_owner_pid == "$owner" && $_acq_active == true ]] || return 97
    [[ $(trap -p EXIT HUP INT TERM) == "$saved_traps" ]] || return 97
}
acquisition_scope --endpoint unix:///outer.sock --bootstrap-image fixture:bootstrap -- outer
''')
        self.assertEqual(0, status, msg=stderr)
        self.assertEqual(b'', stdout)
        self.assertFalse((root / 'identity-arguments').exists())

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
trap 'code=$?; if [[ -f "$receipt" ]] && cmp -s "$receipt" <(printf "retired\n"); then ready=yes; else ready=no; fi; printf "%s %s\n" "$code" "$ready" >> "$STATE/caller-exit"; printf "%s" "$_acq_diagnostic" > "$STATE/owner-diagnostic"; if [[ ${CALLER_EXITS:-0} == 1 ]]; then exit "$code"; fi' EXIT
grants=(--read-only --entrypoint python)
application=(-c ignored)
callback() {
    CALLBACK_BODY
}
if [[ -v CALLER_TERM_TRAP ]]; then trap "$CALLER_TERM_TRAP" TERM; fi
acquisition_scope --endpoint unix:///var/run/docker.sock --bootstrap-image fixture:bootstrap --retired-receipt "$receipt" -- callback
'''.replace('CALLBACK_BODY', callback)
        injected = {}
        if env is not None:
            injected.update(env)
        if 'ACQUISITION_STDOUT_STAT_FAULT' in injected:
            (binary / 'stat').symlink_to(Path(__file__).resolve().parent / 'fixtures/acquisition_engine.sh')
        if 'CLIENT_CLEANUP_FAILURE' in injected:
            (binary / 'rm').symlink_to(Path(__file__).resolve().parent / 'fixtures/acquisition_engine.sh')
        if 'NATIVE_CORRUPT_REMOVAL_ROLE' in injected:
            (binary / 'mv').symlink_to(Path(__file__).resolve().parent / 'fixtures/native_command.py')
            injected.update(NATIVE_CONTROL=str(root / 'native-control'), NATIVE_PUBLICATION_FAILURE='none',
                            NATIVE_STATUS_MUTATION='none', NATIVE_CORRUPT_READY='1')
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

    def test_compose_workers_preserve_stdout_status_and_repeated_scope_calls(self):
        process, root = self.launch(r'''
printf 'services: {}\n' > "$STATE/caller.yml"
for attempt in 1 2; do
    result=unchanged
    acquisition_compose_worker offline result "$STATE/caller.yml" worker-offline || return "$?"
    printf 'result=%s\n' "$result"
done
''', env={'ACQUISITION_COMPOSE_EXIT': '7'})
        self.assertEqual(b'package-output\nresult=7\npackage-output\nresult=7\n',
                         self.assert_exit(process, root, 0))
        calls = (root / 'compose-calls').read_text().splitlines()
        self.assertEqual(['run', 'down', 'run', 'down'], [line.split()[1] for line in calls])
        self.assertNotEqual(calls[0].split()[0], calls[2].split()[0])

    def test_compose_cleanup_failure_blocks_result_and_retirement(self):
        process, root = self.launch(r'''
printf 'services: {}\n' > "$STATE/caller.yml"
result=unchanged
acquisition_compose_worker offline result "$STATE/caller.yml" worker-offline
status=$?
printf 'result=%s\n' "$result"
return "$status"
''', env={'FAIL_COMPOSE_DOWN': '1'})
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(2, process.returncode, msg=stderr)
        self.assertEqual(b'package-output\nresult=unchanged\n', stdout)
        self.assertEqual(['2 no'], (root / 'caller-exit').read_text().splitlines())
        self.assertEqual(1, sum(line.endswith(' down') for line in (root / 'compose-calls').read_text().splitlines()))

    def test_compose_cancellation_runs_scope_teardown(self):
        process, root = self.launch(r'''
printf 'services: {}\n' > "$STATE/caller.yml"
acquisition_compose_worker offline result "$STATE/caller.yml" worker-offline
''', env={'HOLD_COMPOSE_WORKER': '1'})
        self.wait_marker(process, root / 'compose-held')
        process.send_signal(signal.SIGTERM)
        self.assert_exit(process, root, 143)
        self.assertFalse((root / 'compose-worker').exists())

    def wait_marker(self, process, marker):
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f'owner exited before {marker.name}: {process.communicate()}')
            time.sleep(.01)
        self.assertTrue(marker.exists(), msg=f'owner did not create {marker}')

    def rescue_native_removal(self, observed, scope_pid, operation, client):
        # A retained PID alone does not authorize signalling a group.
        anchor = observed['anchor']
        command = Path(f'/proc/{anchor}/cmdline').read_bytes().split(b'\0')[:-1]
        start = Path(f'/proc/{anchor}/stat').read_text().rsplit(')', 1)[1].split()[19]
        self.assertEqual(observed['command'], [os.fsdecode(part) for part in command])
        self.assertEqual(observed['start'], start)
        self.assertEqual(str(Path(observed['directory']) / 'status'), observed['command'][4])
        self.assertEqual(anchor, os.getpgid(anchor))
        self.assertEqual(scope_pid, os.getsid(anchor))
        os.killpg(anchor, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while Path(f'/proc/{anchor}').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(Path(f'/proc/{anchor}').exists())
        for directory in (Path(observed['directory']), operation, client):
            shutil.rmtree(directory)

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

    def test_reporting_cancellation_reaches_restored_caller_disposition(self):
        callback = r'''
python3 -I -S -B - <<'PY'
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
_acq_fail 'scope reporter barrier'
return 2
'''
        cases = (
            ('default', {}, -signal.SIGTERM),
            ('custom', {'CALLER_TERM_TRAP': 'printf "handled\\n" > "$STATE/caller-term"; exit 77'}, 77),
            ('ignored', {'CALLER_TERM_TRAP': ''}, 2),
        )
        for disposition, environment, expected in cases:
            with self.subTest(disposition=disposition):
                process, root = self.launch(callback, env=environment)
                reporter = None
                deadline = time.monotonic() + 15
                while reporter is None and time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), msg='owner exited before blocked reporter')
                    pending = [process.pid]
                    while pending:
                        parent = pending.pop()
                        try:
                            children = Path(f'/proc/{parent}/task/{parent}/children').read_text().split()
                        except FileNotFoundError:
                            continue
                        for child in children:
                            pid = int(child)
                            pending.append(pid)
                            try:
                                argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[:-1]
                                channel = Path(f'/proc/{pid}/wchan').read_text().strip()
                                session = os.getsid(pid)
                            except (FileNotFoundError, ProcessLookupError):
                                continue
                            if (len(argv) == 9 and argv[:5] == [b'python3', b'-I', b'-S', b'-B', b'-c']
                                    and argv[-3:] == [b'scope reporter barrier\n', b'', b'']
                                    and channel in {'pipe_write', 'anon_pipe_write'}
                                    and session == process.pid):
                                reporter = pid
                    if reporter is None:
                        time.sleep(.005)
                self.assertIsNotNone(reporter, msg='no owned blocked reporter observed')
                self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(expected, process.wait(timeout=5))
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(b'', stdout, msg=stderr)
                self.assertFalse(Path(f'/proc/{reporter}').exists())
                self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
                if disposition == 'custom':
                    self.assertEqual('handled\n', (root / 'caller-term').read_text())
                    self.assertEqual(['77 yes'], (root / 'caller-exit').read_text().splitlines())
                elif disposition == 'ignored':
                    self.assertEqual(['2 yes'], (root / 'caller-exit').read_text().splitlines())

    def test_ordinary_reporter_status143_is_not_cancellation(self):
        process, root = self.launch(
            "_acq_fail 'ordinary reporter status'; "
            'inference_native() { inference_cancellation_status=0; return 143; }; return 2')
        self.assert_exit(process, root, 2)
        self.assertIn('stderr delivery failed (status 143', (root / 'owner-diagnostic').read_text())

    def test_failed_readiness_retains_private_causes_after_container_retirement(self):
        cases = (
            ('stopped', b'stopped\n', b'false\n', b'', 1, '0', b'stopped before becoming ready'),
            ('inspect-failed', b'inspect-failed 37\n', b'', b'private inspect cause\n', 1, '23',
             b'running state could not be verified'),
            ('malformed', b'invalid-state\n', b'private malformed state\n', b'', 1, '0',
             b'running state could not be verified'),
            ('exhausted', b'checks-exhausted\n', b'true\n', b'', 100, '37', b'did not become ready'),
        )
        for role in ('gateway', 'relay'):
            for scenario, reason, state, inspect_error, polls, log_status, public_reason in cases:
                with self.subTest(role=role, scenario=scenario):
                    process, root = self.launch(
                        'acquisition_set_route system; '
                        'acquisition_worker online status fixture:local grants application',
                        env={'READINESS_ROLE': role, 'READINESS_SCENARIO': scenario,
                             'READINESS_LOG_STATUS': log_status})
                    stdout, stderr = process.communicate(timeout=20)
                    operation = Path((root / 'scratch').read_text().strip())
                    self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                    self.assertEqual(2, process.returncode, msg=(stdout, stderr))
                    self.assertEqual(b'', stdout)
                    self.assertEqual(['2 yes'], (root / 'caller-exit').read_text().splitlines())
                    self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
                    self.assertEqual([], list(root.glob('live-*')))
                    expected_roles = ['gateway']
                    if role == 'relay':
                        expected_roles.insert(0, 'relay')
                    self.assertEqual(expected_roles, (root / 'removal-attempts').read_text().splitlines())
                    self.assertEqual([role] * polls, (root / 'readiness-inspections').read_text().splitlines())
                    self.assertEqual([role], (root / 'readiness-logs').read_text().splitlines())
                    self.assertIn(str(operation).encode(), stderr)
                    self.assertIn(f'{role} readiness failed'.encode(), stderr)
                    self.assertIn(public_reason, stderr)
                    if scenario == 'inspect-failed':
                        self.assertIn(b'inspect status 37', stderr)
                    if log_status != '0':
                        self.assertIn(f'logs (status {log_status})'.encode(), stderr)
                    for private in (b'private inspect cause', b'private malformed state',
                                    b'private container stdout', b'private container stderr'):
                        self.assertNotIn(private, stdout + stderr)
                    self.assertEqual(0o700, operation.stat().st_mode & 0o777)
                    evidence = operation / role
                    expected = {
                        'readiness-reason': reason, 'state': state, 'state.stderr': inspect_error,
                        'logs': b'private container stdout\n',
                        'logs.stderr': b'private container stderr\n',
                        'log-status': (log_status + '\n').encode(),
                    }
                    for name, contents in expected.items():
                        path = evidence / name
                        self.assertEqual(contents, path.read_bytes(), msg=str(path))
                        self.assertEqual(0o600, path.stat().st_mode & 0o777)
                        self.assertLess(path.stat().st_size, 4096)
                    match = re.search(r'--config (/tmp/nmrnet-docker-client\.[^/]+)/config',
                                      (root / 'engine-client').read_text())
                    self.assertIsNotNone(match)
                    self.assertFalse(Path(match.group(1)).exists())

    def test_private_metadata_receipts_must_remain_below_the_native_file_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt'
            for size, expected_status in ((0, 0), (4095, 0), (4096, 1)):
                with self.subTest(size=size):
                    receipt.write_bytes(b'x' * size)
                    result = subprocess.run(
                        ['bash', '-c', 'source host/acquisition_tool/owner.sh; '
                         '_acq_private_receipt_below_limit "$1"', 'bash', str(receipt)],
                        capture_output=True, timeout=5, check=False)
                    self.assertEqual(expected_status, result.returncode, msg=result.stderr)
                    self.assertEqual(b'', result.stdout)
                    self.assertEqual(b'', result.stderr)

    def test_unknown_or_capped_create_output_size_retains_unadmitted_gateway(self):
        cases = (
            ('failure', {'ACQUISITION_STDOUT_STAT_FAULT': 'failure'}),
            ('nonnumeric', {'ACQUISITION_STDOUT_STAT_FAULT': 'nonnumeric'}),
            ('cap', {'CAP_CREATE_STDOUT_ROLE': 'gateway'}),
        )
        for fault, environment in cases:
            with self.subTest(fault=fault):
                process, root = self.launch(
                    'acquisition_set_route system; '
                    'acquisition_worker online status fixture:local grants application', env=environment)
                stdout, stderr = process.communicate(timeout=20)
                operation = Path((root / 'scratch').read_text().strip())
                self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                match = re.search(r'--config (/tmp/nmrnet-docker-client\.[^/]+)/config',
                                  (root / 'engine-client').read_text())
                self.assertIsNotNone(match)
                private_client = Path(match.group(1))
                self.addCleanup(shutil.rmtree, private_client, ignore_errors=True)
                self.assertEqual(2, process.returncode, msg=(stdout, stderr))
                self.assertEqual(b'', stdout)
                self.assertIn(b'Cannot admit gateway: create output and CID receipt must agree.', stderr)
                self.assertIn(b'Cannot confirm acquisition retirement;', stderr)
                self.assertEqual(['2 no'], (root / 'caller-exit').read_text().splitlines())
                self.assertFalse((root / 'retired').exists())
                self.assertEqual([], list(root.glob('start-*')))
                self.assertEqual([], list(root.glob('rm-*')))
                self.assertFalse((root / 'removal-attempts').exists())
                self.assertEqual('1', (root / 'counter').read_text())
                identifier = f'{1:064x}'
                self.assertEqual({f'live-{identifier}'}, {path.name for path in root.glob('live-*')})
                self.assertTrue(operation.is_dir())
                self.assertTrue(private_client.is_dir())
                self.assertEqual(identifier.encode() + b'\n', (operation / 'gateway/cid').read_bytes())
                receipt = operation / 'gateway/stdout'
                if fault == 'cap':
                    self.assertEqual(4096, receipt.stat().st_size)
                    self.assertEqual(b' ' * 4096, receipt.read_bytes())
                    self.assertFalse((root / 'stat-fault-reached').exists())
                else:
                    self.assertEqual(fault + '\n', (root / 'stat-fault-reached').read_text())
                    self.assertEqual(identifier.encode() + b'\n', receipt.read_bytes())

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

    def test_incomplete_relay_setup_cannot_admit_a_later_worker(self):
        cases = (
            {'WORKER_STATUS': '0', 'READINESS_ROLE': 'relay',
             'READINESS_SCENARIO': 'stopped', 'READINESS_LOG_STATUS': '0'},
            {'WORKER_STATUS': '0', 'INVALID_RELAY_STATE': '1'},
        )
        for environment in cases:
            with self.subTest(environment=environment):
                process, root = self.launch(r'''
acquisition_set_route system
first=0 second=0
acquisition_worker online status fixture:first grants application || first=$?
acquisition_worker online status fixture:second grants application || second=$?
printf '%s %s\n' "$first" "$second"
return "$first"
''', env=environment)
                stdout, stderr = process.communicate(timeout=20)
                operation = Path((root / 'scratch').read_text().strip())
                self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                self.assertEqual(2, process.returncode, msg=(stdout, stderr))
                self.assertEqual(b'2 2\n', stdout, msg=stderr)
                self.assertIn(b'Cannot reuse acquisition route', stderr)
                self.assertIn(b'relay setup did not complete', stderr)
                self.assertEqual('2', (root / 'counter').read_text())
                self.assertFalse((root / 'started-worker').exists())
                self.assertEqual(['relay', 'gateway'], (root / 'removal-attempts').read_text().splitlines())
                self.assertEqual([], list(root.glob('live-*')))
                self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
                if 'READINESS_ROLE' in environment:
                    self.assertIn(b'stopped before becoming ready', stderr)
                if 'INVALID_RELAY_STATE' in environment:
                    self.assertIn(b'relay network and running state could not be verified', stderr)
                    self.assertEqual(b'none false\n', (operation / 'relay/mode').read_bytes())

    def test_ordinary_worker_exit_does_not_invalidate_the_shared_route(self):
        process, root = self.launch(r'''
acquisition_set_route system
acquisition_worker online first fixture:first grants application || return $?
acquisition_worker online second fixture:second grants application || return $?
printf '%s %s\n' "$first" "$second"
''', env={'WORKER_STATUS': '7'})
        self.assertEqual(b'7 7\n', self.assert_exit(process, root, 0))
        self.assertEqual('4', (root / 'counter').read_text())
        self.assertEqual(['worker-1', 'worker-2', 'relay', 'gateway'],
                         (root / 'removal-attempts').read_text().splitlines())

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

    def test_completed_worker_failure_survives_unadmitted_removal_identity(self):
        for status in (37, 143):
            with self.subTest(status=status):
                process, root = self.launch(
                    'acquisition_set_route system; acquisition_worker online status fixture:local grants application '
                    '|| return $?; return "$status"',
                    env={'WORKER_STATUS': str(status), 'CORRUPT_COMPLETED_WORKER_CID': 'damaged-worker'})
                stdout, stderr = process.communicate(timeout=20)
                operation = Path((root / 'scratch').read_text().strip())
                self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                client = Path((root / 'engine-client').read_text().split()[1]).parent
                self.addCleanup(shutil.rmtree, client, ignore_errors=True)
                self.assertEqual(status, process.returncode, msg=(stdout, stderr))
                self.assertTrue(client.is_dir())
                self.assertTrue(operation.is_dir())
                self.assertEqual(b'damaged-worker\n', (operation / 'worker-1/cid').read_bytes())
                self.assertEqual((f'{3:064x}' + '\n').encode(), (operation / 'worker-1/stdout').read_bytes())
                self.assertFalse((root / 'retired').exists())
                self.assertFalse((root / 'removal-attempts').exists())
                self.assertIn(b'worker-1', stderr)
                self.assertIn(str(status).encode(), stderr)
                self.assertEqual([f'{status} no'], (root / 'caller-exit').read_text().splitlines())

    def test_client_cleanup_preserves_primary_status_and_actual_removal_effect(self):
        cases = (
            (0, 'retain', 41, b'Permission denied'),
            (37, 'retain', 37, b'Permission denied'),
            (0, 'delete', 41, b'failed after deleting the selected directory'),
            (37, 'delete', 37, b'failed after deleting the selected directory'),
        )
        for status, effect, expected, cause in cases:
            with self.subTest(status=status, effect=effect):
                process, root = self.launch(
                    'acquisition_set_route system; acquisition_worker online status fixture:local grants application '
                    '|| return $?; return "$status"',
                    env={'WORKER_STATUS': str(status), 'CLIENT_CLEANUP_FAILURE': effect})
                stdout, stderr = process.communicate(timeout=20)
                client = Path((root / 'client-cleanup-path').read_text().strip())
                self.addCleanup(shutil.rmtree, client, ignore_errors=True)
                self.assertEqual(expected, process.returncode, msg=(stdout, stderr))
                self.assertEqual(effect == 'retain', client.is_dir())
                self.assertIn(str(client).encode(), stderr)
                self.assertIn(b'status 41', stderr)
                self.assertIn(cause, stderr)
                if status:
                    self.assertIn(b'already failed with status 37', stderr)
                operation = Path((root / 'scratch').read_text().strip())
                self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                private_cause = operation / 'client-cleanup.stderr'
                self.assertEqual(0o600, private_cause.stat().st_mode & 0o777)
                self.assertIn(b'status 41', private_cause.read_bytes())
                report = operation / 'failure.stderr'
                self.assertEqual(0o600, report.stat().st_mode & 0o777)
                self.assertEqual(stderr, report.read_bytes())
                self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
                self.assertEqual([], list(root.glob('live-*')))
                self.assertEqual([f'{expected} yes'], (root / 'caller-exit').read_text().splitlines())

    def test_client_cleanup_cause_is_reported_without_interrupting_its_producer(self):
        process, root = self.launch(
            'printf "%s\\n" "$_acq_scratch" > "$STATE/scratch"; return 37',
            env={'CLIENT_CLEANUP_FAILURE': 'oversized-cause'})
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(37, process.returncode, msg=(stdout, stderr))
        operation = Path((root / 'scratch').read_text().strip())
        client = Path((root / 'client-cleanup-path').read_text().strip())
        self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
        self.addCleanup(shutil.rmtree, client, ignore_errors=True)
        private_cause = operation / 'client-cleanup.stderr'
        self.assertEqual(0o600, private_cause.stat().st_mode & 0o777)
        self.assertEqual(4097, private_cause.stat().st_size)
        self.assertIn(b'cleanup-cause-canary', private_cause.read_bytes())
        self.assertIn(b'cleanup-cause-canary', stderr)
        self.assertIn(b'status 41', stderr)
        self.assertIn(b'truncated after 4096 bytes', stderr)
        self.assertNotIn(b'cleanup-output-tail', stderr)
        self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
        self.assertTrue(client.is_dir())

    def test_caller_cancellation_does_not_suppress_a_client_cleanup_failure(self):
        for interrupted, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupted=interrupted):
                process, root = self.launch(
                    'acquisition_set_route system; acquisition_worker online result fixture:local grants application',
                    env={'STREAM_WITNESS': '1', 'CLIENT_CLEANUP_FAILURE': 'retain'})
                self.wait_marker(process, root / 'started-worker')
                process.send_signal(interrupted)
                stdout, stderr = process.communicate(timeout=20)
                operation = Path((root / 'scratch').read_text().strip())
                client = Path((root / 'client-cleanup-path').read_text().strip())
                self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                self.addCleanup(shutil.rmtree, client, ignore_errors=True)
                self.assertEqual(status, process.returncode, msg=(stdout, stderr))
                self.assertIn(f'was interrupted (status {status})'.encode(), stderr)
                self.assertIn(b'status 41', stderr)
                self.assertIn(b'Permission denied', stderr)
                self.assertEqual(stderr, (operation / 'failure.stderr').read_bytes())
                self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
                self.assertEqual([], list(root.glob('live-*')))

    def test_client_cleanup_control_bytes_are_escaped_without_losing_raw_evidence(self):
        process, root = self.launch(
            'printf "%s\\n" "$_acq_scratch" > "$STATE/scratch"; return 37',
            env={'CLIENT_CLEANUP_FAILURE': 'binary-cause'})
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(37, process.returncode, msg=(stdout, stderr))
        operation = Path((root / 'scratch').read_text().strip())
        client = Path((root / 'client-cleanup-path').read_text().strip())
        self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
        self.addCleanup(shutil.rmtree, client, ignore_errors=True)
        self.assertTrue((operation / 'client-cleanup.stderr').read_bytes().startswith(b'cleanup\0cause\r\n\n'))
        self.assertIn(b'cleanup\\x00cause\\r\n\n', stderr)
        self.assertNotIn(b'ignored null byte', stderr)
        self.assertNotIn(b'\0', stderr)
        self.assertNotIn(b'truncated after', stderr)

    def test_client_cleanup_failure_finishes_before_public_stderr_is_drained(self):
        callback = r'''
printf '%s\n' "$_acq_scratch" > "$STATE/scratch"
if [[ $CLIENT_CLEANUP_FAILURE == capture-directory ]]; then mkdir -- "$_acq_scratch/client-cleanup.stderr"; fi
python3 -I -S -B - <<'PY'
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
return 37
'''
        for failure in ('retain', 'oversized-cause', 'capture-directory'):
            with self.subTest(failure=failure):
                process, root = self.launch(callback, env={'CLIENT_CLEANUP_FAILURE': failure})
                self.wait_marker(process, root / 'client-cleanup-path')
                operation = Path((root / 'scratch').read_text().strip())
                client = Path((root / 'client-cleanup-path').read_text().strip())
                self.addCleanup(shutil.rmtree, operation, ignore_errors=True)
                self.addCleanup(shutil.rmtree, client, ignore_errors=True)
                self.assertEqual(37, process.wait(timeout=10))
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(b'', stdout)
                self.assertEqual({ord('x')}, set(stderr))
                self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
                self.assertTrue(client.is_dir())
                self.assertIn('stderr delivery failed (status 124', (root / 'owner-diagnostic').read_text())
                report = operation / 'failure.stderr'
                self.assertEqual(0o600, report.stat().st_mode & 0o777)
                detail = report.read_bytes()
                self.assertIn(b'already failed with status 37', detail)
                self.assertIn(b'status 41', detail)
                if failure == 'capture-directory':
                    self.assertIn(b'destination is a directory', detail)
                elif failure == 'oversized-cause':
                    self.assertIn(b'truncated after 4096 bytes', detail)
                else:
                    self.assertIn(b'Permission denied', detail)

    def test_failed_removal_can_settle_absence_without_repeating_the_mutation(self):
        process, root = self.launch(
            'acquisition_set_route system; acquisition_worker online status fixture:local grants application',
            env={'WORKER_STATUS': '0', 'FAIL_REMOVE_AFTER_EFFECT': 'worker-1'})
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(2, process.returncode, msg=(stdout, stderr))
        self.assertIn(b'acquisition worker-1 failed (status 23)', stderr)
        self.assertEqual(['worker-1', 'relay', 'gateway'],
                         (root / 'removal-attempts').read_text().splitlines())
        self.assertEqual([], list(root.glob('live-*')))
        self.assertEqual(b'retired\n', (root / 'retired').read_bytes())
        self.assertEqual(['2 yes'], (root / 'caller-exit').read_text().splitlines())
        self.assertFalse(Path((root / 'scratch').read_text().strip()).exists())

    def test_unknown_native_removal_retains_scope_and_blocks_later_workers(self):
        # Route removal happens after the callback returns. Worker removal
        # happens inside it, allowing a later admission attempt after the fault.
        for selected, roles, expected in (('gateway', ('gateway',), '2 0\n'),
                                           ('relay', ('gateway', 'relay'), '2 0\n'),
                                           ('worker-1', ('gateway', 'relay', 'worker-1'), '2 2\n')):
            with self.subTest(selected=selected):
                environment = {'NATIVE_CORRUPT_REMOVAL_ROLE': selected, 'WORKER_STATUS': '0'}
                if selected != 'worker-1':
                    environment.update(READINESS_ROLE=selected, READINESS_SCENARIO='stopped', READINESS_LOG_STATUS='0')
                process, root = self.launch(r'''
acquisition_set_route system
first=0 second=0
acquisition_worker online status fixture:first grants application || first=$?
if [[ $NATIVE_CORRUPT_REMOVAL_ROLE == worker-1 ]]; then
    acquisition_worker online status fixture:second grants application || second=$?
fi
printf '%s %s\n' "$first" "$second" > "$STATE/callback-status"
return "$first"
''', env=environment)
                self.wait_marker(process, root / 'native-control')
                observed = json.loads((root / 'native-control').read_text())
                anchor = observed['anchor']
                operation = Path((root / 'scratch').read_text().strip())
                client = Path((root / 'engine-client').read_text().split()[1]).parent

                self.addCleanup(self.rescue_native_removal, observed, process.pid, operation, client)
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(2, process.returncode, msg=(stdout, stderr))
                self.assertEqual(['2 no'], (root / 'caller-exit').read_text().splitlines())
                self.assertEqual(expected, (root / 'callback-status').read_text())
                self.assertFalse((root / 'retired').exists())
                self.assertFalse((root / 'removal-attempts').exists())
                self.assertFalse((root / 'absence-probes').exists())
                self.assertEqual([], list(root.glob('rm-*')))
                self.assertEqual(str(len(roles)), (root / 'counter').read_text())
                self.assertTrue(client.is_dir())
                self.assertTrue(Path(observed['directory']).is_dir())
                self.assertEqual(anchor, observed['group'])
                self.assertEqual(process.pid, observed['session'])
                self.assertEqual(['rm', '--force', f'{len(roles):064x}'], observed['command'][-3:])
                for index, role in enumerate(roles, 1):
                    identifier = f'{index:064x}'
                    receipt = operation / role / 'cid'
                    self.assertEqual((identifier + '\n').encode(), receipt.read_bytes())
                    self.assertEqual(receipt.read_bytes(), (operation / role / 'stdout').read_bytes())
                    self.assertTrue((root / f'live-{identifier}').is_file())

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
                self.assertIsNotNone(match)
                self.assertTrue(Path(match.group(1)).is_dir())
                self.assertTrue(operation.is_dir())
                for index, role in enumerate(('gateway', 'relay', 'worker-1'), 1):
                    identifier = f'{index:064x}'
                    receipt = operation / role / 'cid'
                    self.assertEqual((identifier + '\n').encode(), receipt.read_bytes())
                    self.assertEqual(receipt.read_bytes(), (operation / role / 'stdout').read_bytes())
                expected_live = [1, 2]
                if failed == 'worker-1':
                    expected_live.append(3)
                self.assertEqual({f'live-{index:064x}' for index in expected_live},
                                 {path.name for path in root.glob('live-*')})
                self.assertNotEqual(process.returncode, 0, msg=(stdout, stderr))
                self.assertFalse((root / 'retired').exists(), msg=(stdout, stderr, list(root.iterdir())))
                self.assertEqual([f'{process.returncode} no'], (root / 'caller-exit').read_text().splitlines())
                attempts = (root / 'removal-attempts').read_text().splitlines()
                expected_attempts = ['worker-1']
                if failed == 'relay':
                    expected_attempts.append('relay')
                self.assertEqual(expected_attempts, attempts)
                for role in forbidden:
                    self.assertNotIn(role, attempts)


if __name__ == "__main__":
    unittest.main()
