"""Python/shell parity, all-or-nothing projections and structured failures."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from provider_deploy.cli import main
from provider_deploy import Bounds, Problem, ProbeFailure, ProbeResult, ResourceState, Retirement, ProbeHandle
from helpers import CID, OBSERVATION


class CliTests(unittest.TestCase):
    # silent-defaults: allow-contract -- The test helper constructs JSON-mode arguments unless the case requests values output.
    def arguments(self, *, values=False):
        args = ["identity", "--endpoint", "unix:///test.sock", "--image", "approved:local",
                "--interpreter", "/usr/bin/python3", "--user-uid", "1000", "--user-gid", "2000",
                "--uid", "0", "--uid", "1000", "--gid", "2000"]
        # silent-defaults: allow-contract -- Only values-mode cases append the values-output request; JSON cases use the baseline argument fixture.
        return args + (["--values"] if values else [])

    def result(self):
        return ProbeResult(OBSERVATION.uid_map, OBSERVATION.gid_map, CID)

    # silent-defaults: allow-contract -- The fixture begins with an unresolved timeout; each case may select a different failure state/category.
    def failure(self, state=ResourceState.UNRESOLVED, category="timeout"):
        return ProbeFailure(Problem("create", category, "safe explanation"), Retirement(state), ProbeHandle("unix:///test.sock", "b" * 32))

    def call(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_missing_arguments_fail_before_creation(self):
        code, out, _ = self.call(["identity"])
        self.assertEqual(2, code)
        self.assertEqual("known-not-created", json.loads(out)["state"])

    def test_values_parse_failure_keeps_stdout_empty(self):
        code, out, err = self.call(["identity", "--values"])
        self.assertEqual((2, ""), (code, out))
        self.assertFalse(json.loads(err)["ok"])

    def test_complete_operation_receives_caller_selected_policy(self):
        with patch("provider_deploy.cli.probe_identity", side_effect=self.failure()) as operation:
            code, out, _ = self.call(self.arguments())
        request = operation.call_args.args[0]
        self.assertEqual("unix:///test.sock", request.endpoint)
        self.assertEqual("/usr/bin/python3", request.interpreter)
        self.assertEqual(3, code)
        self.assertEqual("create", json.loads(out)["problem"]["stage"])
        self.assertNotIn("uid_map", out)

    def test_values_are_only_requested_decimal_ids_in_order(self):
        with patch("provider_deploy.cli.probe_identity", return_value=self.result()):
            code, out, err = self.call(self.arguments(values=True))
        self.assertEqual((0, "100000 101000 202000\n", ""), (code, out, err))

    def test_duplicate_projections_preserve_order(self):
        with patch("provider_deploy.cli.probe_identity", return_value=self.result()):
            code, out, _ = self.call(self.arguments(values=True) + ["--uid", "0"])
        self.assertEqual(0, code)
        self.assertEqual("100000 101000 100000 202000\n", out)

    def test_abbreviations_are_rejected_without_invoking_operation(self):
        with patch("provider_deploy.cli.probe_identity") as operation:
            code, out, _ = self.call(self.arguments() + ["--val"])
        self.assertEqual(2, code)
        self.assertFalse(json.loads(out)["ok"])
        operation.assert_not_called()

    def test_cli_uses_policy_defaults_and_applies_only_explicit_overrides(self):
        with patch("provider_deploy.cli.probe_identity", return_value=self.result()) as operation:
            self.call(self.arguments())
        request = operation.call_args.args[0]
        self.assertEqual(Bounds(), request.bounds)
        self.assertEqual("default", request.userns_mode)
        with patch("provider_deploy.cli.probe_identity", return_value=self.result()) as operation:
            self.call(self.arguments() + ["--timeout", "7", "--userns", "host"])
        request = operation.call_args.args[0]
        self.assertEqual(replace(Bounds(), operation_seconds=7), request.bounds)
        self.assertEqual("host", request.userns_mode)

    def test_no_requested_ids_means_no_projections(self):
        arguments = ["identity", "--endpoint", "unix:///test.sock", "--image", "approved:local",
                     "--interpreter", "/usr/bin/python3", "--user-uid", "1000", "--user-gid", "2000"]
        with patch("provider_deploy.cli.probe_identity", return_value=self.result()):
            code, out, _ = self.call(arguments)
        self.assertEqual(0, code)
        payload = json.loads(out)
        self.assertEqual([], payload["mapped_uids"])
        self.assertEqual([], payload["mapped_gids"])

    def test_empty_values_projection_prints_only_one_empty_line(self):
        arguments = ["identity", "--endpoint", "unix:///test.sock", "--image", "approved:local",
                     "--interpreter", "/usr/bin/python3", "--user-uid", "1000", "--user-gid", "2000", "--values"]
        with patch("provider_deploy.cli.probe_identity", return_value=self.result()):
            # An earlier invocation's projections must not populate the next one.
            self.call(self.arguments(values=True))
            code, out, err = self.call(arguments)
        self.assertEqual((0, "\n", ""), (code, out, err))

    def test_values_failure_has_empty_stdout_and_structured_stderr(self):
        with patch("provider_deploy.cli.probe_identity", side_effect=self.failure()):
            code, out, err = self.call(self.arguments(values=True))
        self.assertEqual(3, code)
        self.assertEqual("", out)
        self.assertEqual("uncertain-outcome", json.loads(err)["state"])

    def test_unmapped_projection_does_not_print_partial_values(self):
        with patch("provider_deploy.cli.probe_identity", return_value=self.result()):
            code, out, err = self.call(self.arguments(values=True) + ["--gid", "999999"])
        self.assertEqual((1, ""), (code, out))
        self.assertEqual("project", json.loads(err)["problem"]["stage"])
        self.assertEqual("confirmed-retired", json.loads(err)["state"])

    def test_sighup_requests_cancellation_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGHUP)
        def operation(request, *, cancel):
            signal.raise_signal(signal.SIGHUP)
            self.assertTrue(cancel.is_set())
            raise self.failure(ResourceState.RETIRED, "cancelled")
        with patch("provider_deploy.cli.probe_identity", side_effect=operation):
            self.assertEqual(130, self.call(self.arguments())[0])
        self.assertEqual(previous, signal.getsignal(signal.SIGHUP))

    def test_help_is_real_and_defaults_do_not_require_timing_knobs(self):
        result = subprocess.run([sys.executable, "-m", "provider_deploy", "identity", "--help"],
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
