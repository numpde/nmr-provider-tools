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
import sys
import tempfile
import time
import unittest

ENGINE_STUB = r'''#!/bin/bash
set -eu
exec 2>>"$ENGINE_STATE/engine-error"
printf '%s\n' "$*" >> "$ENGINE_STATE/engine-calls"
[[ $# -ge 5 && "$1" == --config && "$3" == --host ]] || exit 97
shift 4
operation=$1
shift
case "$operation" in
  create)
    while [[ "$1" != --cidfile ]]; do shift; done
    receipt=$2
    role=${receipt%/cid}
    role=${role##*/}
    scratch=${receipt%/$role/cid}
    count=0
    [[ ! -f "$ENGINE_STATE/counter" ]] || count=$(<"$ENGINE_STATE/counter")
    count=$((count+1))
    printf '%s' "$count" > "$ENGINE_STATE/counter"
    printf -v identifier '%064x' "$count"
    printf '%s\n' "$identifier" > "$receipt"
    : > "$ENGINE_STATE/live-$identifier"
    printf '%s\n' "$role" > "$ENGINE_STATE/role-$identifier"
    printf '%s\n' "$scratch" > "$ENGINE_STATE/scratch"
    printf '%s\n' "$identifier"
    ;;
  inspect)
    identifier=${!#}
    role=$(<"$ENGINE_STATE/role-$identifier")
    case "$2" in
      '{{.HostConfig.NetworkMode}}')
        if [[ $role == worker-* && -f "$ENGINE_STATE/relay-id" ]]; then
          printf 'container:%s\n' "$(<"$ENGINE_STATE/relay-id")"
        elif [[ $role == gateway ]]; then printf 'host\n'; else printf 'none\n'; fi ;;
      '{{.HostConfig.NetworkMode}} {{.State.Running}}') printf 'none true\n' ;;
      '{{.State.Running}} {{.State.ExitCode}}') printf 'false %s\n' "${WORKER_STATUS:-0}" ;;
      *) exit 97 ;;
    esac
    ;;
  start)
    identifier=${!#}
    role=$(<"$ENGINE_STATE/role-$identifier")
    scratch=$(<"$ENGINE_STATE/scratch")
    if [[ $role == gateway ]]; then
      python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.close()' "$scratch/gateway-socket/proxy.sock"
      printf '/gateway/proxy.sock\n' > "$scratch/gateway-ready/socket"
      exit 0
    elif [[ $role == relay ]]; then
      printf '%s\n' "$identifier" > "$ENGINE_STATE/relay-id"
      printf '12345\n' > "$scratch/relay-ready/port"
      exit 0
    fi
    : > "$ENGINE_STATE/started-worker"
    if [[ ${STREAM_WITNESS:-0} == 1 ]]; then
      printf 'streamed\n'
      while [[ ! -f "$ENGINE_STATE/release-worker" ]]; do sleep 0.01; done
    fi
    if [[ -n ${WORKER_STATUS+x} ]]; then exit "$WORKER_STATUS"; fi
    while :; do sleep 1; done
    ;;
  rm)
    identifier=${!#}
    role=$(<"$ENGINE_STATE/role-$identifier")
    printf '%s\n' "$role" >> "$ENGINE_STATE/removal-attempts"
    if [[ $role == ${FAIL_REMOVE:-} ]]; then exit 1; fi
    rm -f "$ENGINE_STATE/live-$identifier"
    : > "$ENGINE_STATE/removed-worker"
    ;;
  ps)
    for arg in "$@"; do
      case "$arg" in id=*) identifier=${arg#id=} ;; esac
    done
    [[ ! -f "$ENGINE_STATE/live-$identifier" ]] || printf '%s\n' "$identifier"
    ;;
  *) exit 97 ;;
esac
'''


class AcquisitionScopeTests(unittest.TestCase):
    def launch(self, callback, *, env=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        binary = root / 'bin'
        binary.mkdir()
        docker = binary / 'docker'
        # Execute checked-in source through a symlink; scratch remains noexec.
        docker.symlink_to(Path(__file__).resolve())
        script = r'''
set -uo pipefail
source acquisition_tool/owner.sh
# Replace only the public mapping port: the scope, workers, native client
# supervisor, signal handlers and engine retirement remain real.
acquisition_map_owner() { printf -v "$1" '%s' 0; printf -v "$2" '%s' 0; }
receipt="$STATE/retired"
trap 'code=$?; if [[ -f "$receipt" ]] && cmp -s "$receipt" <(printf "retired\n"); then ready=yes; else ready=no; fi; printf "%s %s\n" "$code" "$ready" >> "$STATE/caller-exit"; if [[ ${CALLER_EXITS:-0} == 1 ]]; then exit "$code"; fi' EXIT
grants=(--read-only --entrypoint python)
application=(-c ignored)
callback() {
    CALLBACK_BODY
}
acquisition_scope --endpoint unix:///var/run/docker.sock --bootstrap-image fixture:local --retired-receipt "$receipt" -- callback
'''.replace('CALLBACK_BODY', callback)
        environment = {**os.environ, 'PATH': f'{binary}:{os.environ["PATH"]}',
                       'STATE': str(root), 'ENGINE_STATE': str(root), **(env or {})}
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
        self.assertTrue(marker.exists(), f'owner did not create {marker}')

    def assert_exit(self, process, root, status):
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(status, process.returncode, (stdout, stderr))
        self.assertEqual([f'{status} yes'], (root / 'caller-exit').read_text().splitlines())
        return stdout

    def test_callback_return_and_exit_preserve_status_and_retire_before_caller_exit(self):
        for action in ('return 2', 'exit 2'):
            with self.subTest(action=action):
                process, root = self.launch(action)
                self.assert_exit(process, root, 2)

    def test_term_to_parent_during_worker_retires_then_chains_caller_exit(self):
        process, root = self.launch('acquisition_worker offline status fixture:local grants application')
        self.wait_marker(process, root / 'started-worker')
        process.send_signal(signal.SIGTERM)
        self.assert_exit(process, root, 143)
        self.assertTrue((root / 'removed-worker').exists())

    def test_worker_result_names_do_not_collide(self):
        callback = r'''
local status=91 report_status=92
local -a engine=(false)
local inference_retirement_confirmed=false inference_cancellation_status=999
acquisition_worker offline status fixture:local grants application || return $?
acquisition_worker offline report_status fixture:local grants application || return $?
printf '%s %s\n' "$status" "$report_status"
'''
        process, root = self.launch(callback, env={'WORKER_STATUS': '7'})
        self.assertIn(b'7 7\n', self.assert_exit(process, root, 0))

    def test_worker_stdout_is_streamed_before_worker_finishes(self):
        process, root = self.launch(
            'acquisition_worker offline status fixture:local grants application',
            env={'STREAM_WITNESS': '1', 'WORKER_STATUS': '0'})
        self.wait_marker(process, root / 'started-worker')
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, 'worker output was buffered until completion')
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
        self.assertEqual(2, process.returncode, (stdout, stderr))
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
                                  (root / 'engine-calls').read_text())
                if match:
                    self.addCleanup(shutil.rmtree, Path(match.group(1)), ignore_errors=True)
                self.assertNotEqual(process.returncode, 0, (stdout, stderr))
                self.assertFalse((root / 'retired').exists(), (stdout, stderr, list(root.iterdir())))
                self.assertEqual([f'{process.returncode} no'], (root / 'caller-exit').read_text().splitlines())
                attempts = (root / 'removal-attempts').read_text().splitlines()
                self.assertIn(failed, attempts)
                for role in forbidden:
                    self.assertNotIn(role, attempts)


if __name__ == '__main__':
    if Path(sys.argv[0]).name == "docker":
        os.execv("/bin/bash", ["bash", "-euc", ENGINE_STUB, "bash", *sys.argv[1:]])
    else:
        unittest.main()
