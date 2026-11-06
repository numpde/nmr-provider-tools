"""An installed CLI runner must not count a nested runner's diagnostics twice."""

import subprocess
import sys
import unittest
from pathlib import Path


class RunnerEntrypointTests(unittest.TestCase):
    def test_nested_asyncio_errors_are_owned_once_via_installed_entrypoint(self):
        runner = Path(__file__).resolve().parents[2] / "run_suite.py"
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(runner),
             "unit/normative", "test_test_runner_summary.py"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertNotIn("PROVIDER-CORE FAILURE SUMMARY", completed.stderr)
