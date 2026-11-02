"""Python/shell parity, all-or-nothing projections and structured failures."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

import linux_idmap
from linux_idmap import IdMap
from linux_idmap._cli import main
from linux_idmap.probe import Bounds, Problem, ProbeFailure, ProbeResult, ResourceState, Retirement, ProbeHandle
from helpers import CID, OBSERVATION, Engine, Guard


class CliTests(unittest.TestCase):
    # silent-defaults: allow-contract -- The test helper constructs JSON-mode arguments unless the case requests values output.
    def arguments(self, *, values=False):
        args = ["--endpoint", "unix:///test.sock", "--image", "approved:local",
                "--interpreter", "/usr/bin/python3", "--user-uid", "1000", "--user-gid", "2000",
                "--uid", "0", "--uid", "1000", "--gid", "2000"]
        # silent-defaults: allow-contract -- Only values-mode cases append the values-output request; JSON cases use the baseline argument fixture.
        return args + (["--values"] if values else [])

    def result(self):
        return ProbeResult(OBSERVATION.uid_map, OBSERVATION.gid_map, CID)

    # silent-defaults: allow-contract -- The fixture begins with an unresolved timeout; each case may select a different failure state/category.
    def failure(self, state=ResourceState.UNRESOLVED, category="timeout"):
        return ProbeFailure(Problem(category, "safe explanation"), Retirement(state), ProbeHandle("unix:///test.sock", "b" * 32))

    def call(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_missing_arguments_fail_before_creation(self):
        code, out, _ = self.call([])
        self.assertEqual(2, code)
        self.assertEqual("known-not-created", json.loads(out)["state"])

    def test_values_parse_failure_keeps_stdout_empty(self):
        code, out, err = self.call(["--values"])
        self.assertEqual((2, ""), (code, out))
        self.assertFalse(json.loads(err)["ok"])

    def test_policy_refusal_uses_the_same_failure_shape_as_operation_errors(self):
        for values in (False, True):
            with self.subTest(values=values):
                code, out, err = self.call(['--values'] if values else [])
                payload = json.loads(err if values else out)
                self.assertEqual(2, code)
                self.assertEqual(set(self.failure().as_dict()), set(payload))
                self.assertEqual('known-not-created', payload['state'])
                self.assertIsNone(payload['container_id'])
                self.assertIsNone(payload['handle'])
                self.assertEqual([], payload['problems'])
                if values:
                    self.assertEqual('', out)

    def test_complete_operation_receives_caller_selected_policy(self):
        with patch("linux_idmap._cli.probe_identity", side_effect=self.failure()) as operation:
            code, out, _ = self.call(self.arguments())
        request = operation.call_args.args[0]
        self.assertEqual("unix:///test.sock", request.endpoint)
        self.assertEqual("/usr/bin/python3", request.interpreter)
        self.assertEqual(3, code)
        self.assertEqual("timeout", json.loads(out)["problem"]["category"])
        self.assertNotIn("uid_map", out)

    def test_default_cli_creates_and_verifies_confinement(self):
        for drift in (None, ('NetworkMode', 'host'), ('ReadonlyRootfs', False),
                      ('CapDrop', []), ('SecurityOpt', [])):
            with self.subTest(drift=drift):
                engine = Engine()
                if drift is not None:
                    engine.on_create = lambda current: current.container.isolation.update([drift])
                with patch('linux_idmap._probe_scope.DockerEngine.connect', return_value=engine), \
                     patch('linux_idmap._probe_scope.ProbeProcesses', return_value=Guard()):
                    code, out, err = self.call(self.arguments())
                for flag, value in (('--network', 'none'), ('--cap-drop', 'ALL'),
                                    ('--security-opt', 'no-new-privileges:true')):
                    self.assertEqual(value, engine.arguments[engine.arguments.index(flag) + 1])
                self.assertIn('--read-only', engine.arguments)
                payload = json.loads(out)
                if drift is None:
                    self.assertEqual(0, code, err)
                    self.assertEqual([100000, 101000], payload['mapped_uids'])
                else:
                    self.assertNotEqual(0, code)
                    self.assertEqual('confinement', payload['problem']['category'])
                    self.assertNotIn('start', engine.events)
                    self.assertNotIn('uid_map', payload)

    def test_confinement_flags_are_not_caller_options(self):
        for flag in ('--confine', '--no-confine'):
            with self.subTest(flag=flag), patch('linux_idmap._cli.probe_identity') as operation:
                code, out, _ = self.call(self.arguments() + [flag])
                self.assertEqual(2, code)
                self.assertEqual('known-not-created', json.loads(out)['state'])
                operation.assert_not_called()

    def test_values_are_only_requested_decimal_ids_in_order(self):
        with patch("linux_idmap._cli.probe_identity", return_value=self.result()):
            code, out, err = self.call(self.arguments(values=True))
        self.assertEqual((0, "100000 101000 202000\n", ""), (code, out, err))

    def test_duplicate_projections_preserve_order(self):
        with patch("linux_idmap._cli.probe_identity", return_value=self.result()):
            code, out, _ = self.call(self.arguments(values=True) + ["--uid", "0"])
        self.assertEqual(0, code)
        self.assertEqual("100000 101000 100000 202000\n", out)

    def test_abbreviations_are_rejected_without_invoking_operation(self):
        with patch("linux_idmap._cli.probe_identity") as operation:
            code, out, _ = self.call(self.arguments() + ["--val"])
        self.assertEqual(2, code)
        self.assertFalse(json.loads(out)["ok"])
        operation.assert_not_called()

    def test_cli_uses_policy_defaults_and_applies_only_explicit_overrides(self):
        with patch("linux_idmap._cli.probe_identity", return_value=self.result()) as operation:
            self.call(self.arguments())
        request = operation.call_args.args[0]
        self.assertEqual(Bounds(), request.bounds)
        self.assertEqual("default", request.userns_mode)
        with patch("linux_idmap._cli.probe_identity", return_value=self.result()) as operation:
            self.call(self.arguments() + ["--timeout", "7", "--userns", "host"])
        request = operation.call_args.args[0]
        self.assertEqual(replace(Bounds(), operation_seconds=7), request.bounds)
        self.assertEqual("host", request.userns_mode)

    def test_no_requested_ids_means_no_projections(self):
        arguments = ["--endpoint", "unix:///test.sock", "--image", "approved:local",
                     "--interpreter", "/usr/bin/python3", "--user-uid", "1000", "--user-gid", "2000"]
        with patch("linux_idmap._cli.probe_identity", return_value=self.result()):
            code, out, _ = self.call(arguments)
        self.assertEqual(0, code)
        payload = json.loads(out)
        self.assertEqual([], payload["mapped_uids"])
        self.assertEqual([], payload["mapped_gids"])

    def test_empty_values_projection_prints_only_one_empty_line(self):
        arguments = ["--endpoint", "unix:///test.sock", "--image", "approved:local",
                     "--interpreter", "/usr/bin/python3", "--user-uid", "1000", "--user-gid", "2000", "--values"]
        with patch("linux_idmap._cli.probe_identity", return_value=self.result()):
            # An earlier invocation's projections must not populate the next one.
            self.call(self.arguments(values=True))
            code, out, err = self.call(arguments)
        self.assertEqual((0, "\n", ""), (code, out, err))

    def test_values_failure_has_empty_stdout_and_structured_stderr(self):
        with patch("linux_idmap._cli.probe_identity", side_effect=self.failure()):
            code, out, err = self.call(self.arguments(values=True))
        self.assertEqual(3, code)
        self.assertEqual("", out)
        self.assertEqual("uncertain-outcome", json.loads(err)["state"])

    def test_unmapped_projection_does_not_print_partial_values(self):
        with patch("linux_idmap._cli.probe_identity", return_value=self.result()):
            code, out, err = self.call(self.arguments(values=True) + ["--gid", "999999"])
        self.assertEqual((1, ""), (code, out))
        self.assertEqual("unmapped-id", json.loads(err)["problem"]["category"])
        self.assertEqual("confirmed-retired", json.loads(err)["state"])

    def test_sighup_requests_cancellation_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGHUP)
        def operation(request, *, cancel):
            signal.raise_signal(signal.SIGHUP)
            self.assertTrue(cancel.is_set())
            raise self.failure(ResourceState.RETIRED, "cancelled")
        with patch("linux_idmap._cli.probe_identity", side_effect=operation):
            self.assertEqual(130, self.call(self.arguments())[0])
        self.assertEqual(previous, signal.getsignal(signal.SIGHUP))

    def test_signal_during_projection_prevents_success_output(self):
        translate = IdMap.translate
        previous = signal.getsignal(signal.SIGTERM)
        def cancel_after_projection(mapping, identifier):
            value = translate(mapping, identifier)
            signal.raise_signal(signal.SIGTERM)
            return value
        for values in (False, True):
            with self.subTest(values=values):
                with patch('linux_idmap._cli.probe_identity', return_value=self.result()), \
                     patch.object(IdMap, 'translate', cancel_after_projection):
                    code, out, err = self.call(self.arguments(values=values))
                self.assertEqual(130, code)
                if values:
                    self.assertEqual('', out)
                payload = json.loads(err if values else out)
                self.assertEqual('cancelled', payload['problem']['category'])
                self.assertEqual('confirmed-retired', payload['state'])
                self.assertEqual(CID, payload['container_id'])
                self.assertNotIn('uid_map', payload)
                self.assertNotIn('mapped_uids', payload)
        self.assertEqual(previous, signal.getsignal(signal.SIGTERM))

    def test_signal_handler_is_lock_free_and_accepts_reentrant_signals(self):
        program = r'''from contextlib import ExitStack
import faulthandler
import signal
import sys
import threading
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from linux_idmap import _cli
from linux_idmap.probe import Problem, ProbeFailure, ResourceState, Retirement

faulthandler.dump_traceback_later(4)
previous = {number: signal.getsignal(number)
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
events = []
make_event = threading.Event

def record_event():
    event = make_event()
    events.append(event)
    return event

def operation(request, *, cancel):
    if sys.argv[2] == 'held-event-lock':
        # Reproduce signal delivery inside Event.wait/set's critical section.
        with ExitStack() as locks:
            for event in events:
                locks.enter_context(event._cond)
            signal.raise_signal(signal.SIGTERM)
    else:
        handler = signal.getsignal(signal.SIGTERM)
        code = getattr(handler, '__func__', handler).__code__
        nested = False
        def trace(frame, event, arg):
            nonlocal nested
            if frame.f_code is code and event == 'call' and not nested:
                nested = True
                signal.raise_signal(signal.SIGTERM)
            return trace
        try:
            sys.settrace(trace)
            signal.raise_signal(signal.SIGTERM)
        finally:
            sys.settrace(None)
        assert nested
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGHUP)
    assert cancel.is_set()
    raise ProbeFailure(Problem('cancelled', 'signal observed'),
                       Retirement(ResourceState.RETIRED))

with patch.object(threading, 'Event', record_event), patch.object(_cli, 'probe_identity', operation):
    status = _cli.main(['--endpoint', 'unix:///test.sock', '--image', 'approved:local',
                        '--interpreter', '/usr/bin/python3', '--user-uid', '0',
                        '--user-gid', '0', '--values'])
assert status == 130
assert all(signal.getsignal(number) is handler for number, handler in previous.items())
faulthandler.cancel_dump_traceback_later()
'''
        for mode in ('held-event-lock', 'reentrant'):
            with self.subTest(mode=mode):
                try:
                    result = subprocess.run(
                        [sys.executable, '-I', '-S', '-B', '-c', program,
                         str(Path(linux_idmap.__file__).parent.parent), mode],
                        capture_output=True, text=True, timeout=10,
                    )
                except subprocess.TimeoutExpired as error:
                    self.fail(f'signal handler did not return: {error.stderr!r}')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('', result.stdout)
                self.assertEqual('cancelled', json.loads(result.stderr)['problem']['category'])

    def test_help_is_real_and_defaults_do_not_require_timing_knobs(self):
        result = subprocess.run([sys.executable, "-m", "linux_idmap._cli", "--help"],
                                capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, result.returncode)
        self.assertIn("--endpoint", result.stdout)
        self.assertNotIn("--cleanup-seconds", result.stdout)

    def test_integer_syntax_rejects_signs_underscores_and_sentinel(self):
        for value in ("+1", "1_000", "-1", "4294967295", "12345678901"):
            args = self.arguments()
            args[args.index("--user-uid") + 1] = value
            with self.subTest(value=value):
                self.assertEqual(2, self.call(args)[0])



if __name__ == "__main__":
    unittest.main()
