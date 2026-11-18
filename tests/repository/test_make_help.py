"""The Make command catalog is complete and release arguments are actionable."""

from tools.container_guard import require_container

require_container()

import re
import subprocess
import unittest

from tests.repository.quality.sources import ROOT


class MakeHelpTests(unittest.TestCase):
    def test_every_maintained_target_is_documented(self):
        content = (ROOT / "Makefile").read_text()
        declared = set(re.findall(r"(?m)^([A-Za-z][A-Za-z0-9/._-]+):(?:\s|$)", content))

        completed_process = subprocess.run(
            ("make", "--no-print-directory", "help"),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(0, completed_process.returncode, msg=completed_process.stderr)
        documented = set(re.findall(r"\bmake ([A-Za-z][A-Za-z0-9/._-]+)", completed_process.stdout))

        self.assertEqual(declared, documented)
        self.assertNotIn("Select a local Docker context", completed_process.stderr)

    def test_release_help_explains_input_output_and_nonoverwriting_flow(self):
        completed_process = subprocess.run(
            ("make", "--no-print-directory", "help"),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(0, completed_process.returncode, msg=completed_process.stderr)

        for phrase in (
            "publish/prepare",
            "publish/release",
            "WHEELHOUSE=",
            "OUTPUT=",
            "ARTIFACTS=",
            "mkdir -m 700",
            "release-artifacts/",
            "empty",
            "0700",
            "review",
            "separate",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, completed_process.stdout)
