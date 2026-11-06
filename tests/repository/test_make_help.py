"""The Make command catalog is complete and release arguments are actionable."""

from tools.container_guard import require_container

require_container()

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]


class MakeHelpTests(unittest.TestCase):
    def test_every_maintained_target_is_documented(self):
        content = (ROOT / "Makefile").read_text()
        declared = set(re.findall(r"(?m)^([A-Za-z][A-Za-z0-9/._-]+):(?:\s|$)", content))
        result = subprocess.run(
            ("make", "--no-print-directory", "help"),
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        documented = set(re.findall(r"\bmake ([A-Za-z][A-Za-z0-9/._-]+)", result.stdout))
        self.assertEqual(declared, documented)
        self.assertNotIn("Select a local Docker context", result.stderr)

    def test_release_help_explains_input_output_and_nonoverwriting_flow(self):
        result = subprocess.run(
            ("make", "--no-print-directory", "help"),
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        for phrase in (
            "publish/prepare", "publish/release", "WHEELHOUSE=", "OUTPUT=",
            "ARTIFACTS=", "mkdir -m 700", "release-artifacts/",
            "empty", "0700", "review", "separate",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, result.stdout)
