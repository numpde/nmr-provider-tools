"""A noncooperative model exchange cannot require restarting its provider lane."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class ModelProcessRecoveryTests(unittest.TestCase):
    def test_hung_model_cleanup_allows_next_job_with_same_warm_worker(self):
        fixture = Path(__file__).resolve().parents[3] / "fakes/chat_process_fault.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "verdict.json"
            process = subprocess.Popen(
                (sys.executable, "-I", "-B", str(fixture), "scenario",
                 str(root / "fault.jsonl"), str(report_path)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            report = None
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if report_path.exists():
                        report = json.loads(report_path.read_text())
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
            finally:
                # This group contains only the disposable fixture and its owned
                # children. Stop it after the behavioral verdict, including the
                # intentionally noncooperative baseline cleanup.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                # silent-defaults: allow-contract -- An absent disposable group needs no signal; communicate still observes the original fixture process.
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=0.5)
                # silent-defaults: allow-contract -- An unobserved exit advances to SIGKILL below; the bounded outer teardown never treats it as recovery.
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                # silent-defaults: allow-contract -- An already absent group needs no further signal; the original fixture is still reaped by communicate.
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate(timeout=2)

            self.assertIsNotNone(report, msg="The bounded recovery fixture did not produce a verdict:\n" + stdout + stderr)
            evidence = json.dumps(report, sort_keys=True)
            self.assertIn("warmup", report["finished_jobs"], msg=evidence)
            self.assertTrue(report["fault"]["read_cancelled"], msg="The HTTP cancellation fault did not engage: " + evidence)
            self.assertTrue(report["fault"]["cleanup_engaged"], msg="The original HTTP cleanup was not held: " + evidence)
            self.assertEqual(1, report["peer_paths"].count("/hang"), msg=evidence)
            self.assertIn("TimeoutError", report["failure_types"], msg=evidence)
            self.assertTrue(
                report["next_job_completed"],
                msg="The affected lane did not serve its next Job inside the original recovery budget: " + evidence,
            )
            self.assertLessEqual(report["next_job_seconds"], report["recovery_budget_seconds"], msg=evidence)
            self.assertEqual(["warmup", "hung", "next"], report["finished_jobs"], msg=evidence)
            self.assertEqual(["/ready", "/hang", "/ready"], report["peer_paths"], msg=evidence)
            self.assertEqual([1, 1, 1], report["session_preparations"], msg=evidence)
            self.assertEqual([1, 0, 1], report["session_executions"], msg=evidence)
            self.assertEqual(2, report["tool_calls"], msg=evidence)
            self.assertEqual(0, report["claim_count"], msg=evidence)
            self.assertEqual(["complete", "fail", "complete"], report["deliveries"], msg=evidence)
            before, after = report["warm_worker_before"], report["warm_worker_after"]
            self.assertEqual(before["pid"], after["pid"], msg=evidence)
            self.assertEqual(1, after["model_loads"], msg=evidence)
            self.assertEqual((2, 2, 3), (after["preparations"], after["executions"], after["retirements"]), msg=evidence)
            self.assertNotEqual(report["provider_pid"], report["fault"]["pid"], msg=evidence)
            self.assertTrue(report["fault"]["term_ignored"], msg="The child did not exercise TERM/KILL escalation: " + evidence)
            self.assertFalse(report["fault"]["owner_still_alive"], msg="The original HTTP owner was not reaped: " + evidence)
