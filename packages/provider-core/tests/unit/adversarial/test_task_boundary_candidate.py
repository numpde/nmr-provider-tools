"""Scripted Python process exits cannot abort owned cleanup or select FAIL."""

import subprocess
import sys
import unittest
from pathlib import Path


class TaskBoundaryCandidateTests(unittest.TestCase):
    def test_scripted_fatal_native_observers_allow_owned_cleanup(self):
        witness = Path(__file__).resolve().parents[2] / "fakes/task_boundary_witness.py"
        for owner in ("executor", "controller"):
            for failure in ("system-exit", "keyboard-interrupt", "ordinary", "cancel"):
                with self.subTest(owner=owner, failure=failure):
                    completed = subprocess.run(
                        [sys.executable, "-I", "-B", str(witness), owner, failure],
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    self.assertEqual(completed.stderr, "")
                    self.assertIn("independent cleanup, no terminal submission", completed.stdout)

    def test_fatal_owned_ports_preserve_cleanup_and_report_duties(self):
        witness = Path(__file__).resolve().parents[2] / "fakes/task_boundary_witness.py"
        for stage in ("retirement", "handoff", "condition"):
            for failure in ("system-exit", "keyboard-interrupt"):
                with self.subTest(stage=stage, failure=failure):
                    completed = subprocess.run(
                        [sys.executable, "-I", "-B", str(witness), stage, failure],
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    self.assertEqual(completed.stderr, "")
                    self.assertIn("exact fatal retained, independent cleanup", completed.stdout)

    def test_fatal_cleanup_preserves_primary_and_cancellation_priority(self):
        witness = Path(__file__).resolve().parents[2] / "fakes/task_boundary_witness.py"
        for owner in ("executor", "controller"):
            for primary in ("cancel", "ordinary", "system-exit"):
                for cleanup in ("system-exit", "keyboard-interrupt"):
                    with self.subTest(owner=owner, primary=primary, cleanup=cleanup):
                        failure = primary + "-cleanup-" + cleanup
                        completed = subprocess.run(
                            [sys.executable, "-I", "-B", str(witness), owner, failure],
                            capture_output=True, text=True, timeout=10,
                        )
                        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                        self.assertEqual(completed.stderr, "")
                        self.assertIn("independent cleanup, no terminal submission", completed.stdout)


    def test_outward_evidence_retains_original_causes_and_independent_cleanup(self):
        witness = Path(__file__).resolve().parents[2] / "fakes/task_boundary_witness.py"
        cases = (
            ("graph-run", "fresh"),
            ("graph-run", "chained"),
            ("graph-run", "multiple"),
            ("graph-settle", "multiple"),
            ("graph-admission", "fresh"),
            ("graph-admission", "cancel"),
            ("graph-controller-delivery", "fresh"),
            ("graph-controller-delivery", "cancel"),
        )
        for stage, variant in cases:
            with self.subTest(stage=stage, variant=variant):
                completed = subprocess.run(
                    [sys.executable, "-I", "-B", str(witness), stage, variant],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertEqual(completed.stderr, "")
                self.assertIn("outward evidence retains independent cleanup and original causes", completed.stdout)
