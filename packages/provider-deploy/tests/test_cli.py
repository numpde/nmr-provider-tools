"""CLI parity, bounded structured failures, and real no-mutation host rejection."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from provider_deploy.cli import main
from provider_deploy.model import ProbeFailure


class CliTests(unittest.TestCase):
    def arguments(self, path):
        return ["identity", "--context", "selected", "--image", "approved:local", "--user-uid", "1000",
                "--user-gid", "2000", "--userns", "default", "--map-uid", "0", "--map-gid", "2000",
                "--journal", str(path), "--operation-seconds", "10", "--command-seconds", "1",
                "--cleanup-seconds", "2", "--lifetime-seconds", "30"]

    def test_invalid_arguments_are_structured_known_not_created(self):
        out = io.StringIO()
        with redirect_stdout(out):
            result = main(["identity"])
        self.assertEqual(2, result)
        self.assertEqual("known-not-created", json.loads(out.getvalue())["state"])
        self.assertEqual("invalid-policy", json.loads(out.getvalue())["category"])

    def test_complete_operation_receives_explicit_policy_and_failure_is_preserved(self):
        out, err = io.StringIO(), io.StringIO()
        failure = ProbeFailure(category="timeout", message="identity probe failed", state="uncertain-outcome",
                               token="abc", container_id=None, journal_path="/private/record", cleanup_failures=("reconciliation",))
        with patch("provider_deploy.cli.probe_identity", side_effect=failure) as operation:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(self.arguments(Path("/private/record")))
        request = operation.call_args.args[0]
        self.assertEqual("selected", request.context)
        self.assertEqual((0,), request.requested_uids)
        self.assertEqual((2000,), request.requested_gids)
        self.assertEqual(3, code)
        self.assertEqual("uncertain-outcome", json.loads(out.getvalue())["state"])
        self.assertNotIn("mapped_uids", out.getvalue())
        self.assertIn("no mapping", err.getvalue())

    def test_output_limit_never_turns_uncertainty_into_retirement(self):
        out = io.StringIO()
        failure = ProbeFailure(category="timeout", message="failed", state="uncertain-outcome",
            token="abc", container_id=None, journal_path="/" + "x" * 140000, cleanup_failures=())
        with patch("provider_deploy.cli.probe_identity", side_effect=failure):
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = main(self.arguments(Path("/private/record")))
        payload = json.loads(out.getvalue())
        self.assertEqual(3, code)
        self.assertEqual("uncertain-outcome", payload["state"])
        self.assertEqual("abc", payload["operation_token"])
        self.assertLess(len(out.getvalue()), 1024)

    def test_module_entrypoint_help_is_real(self):
        result = subprocess.run([sys.executable, "-m", "provider_deploy", "identity", "--help"],
                                capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, result.returncode)
        self.assertIn("--cleanup-seconds", result.stdout)
        self.assertEqual("", result.stderr)

    def test_integer_notation_does_not_accept_python_underscores_or_signs(self):
        for value in ("+1", "1_000", "-1", "12345678901"):
            with self.subTest(value=value):
                args = self.arguments(Path("/private/record"))
                args[args.index("--user-uid") + 1] = value
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(2, main(args))


if __name__ == "__main__":
    unittest.main()
