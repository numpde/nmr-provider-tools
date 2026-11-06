"""Seeded interpreter timeouts preserve the original result publisher and science."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class RepeatedInterpreterTimeoutTests(unittest.TestCase):
    def test_timeouts_beyond_job_capacity_preserve_the_original_jobs_api_receipt(self):
        fixture = Path(__file__).resolve().parents[3] / "fakes/b3_repetition_fault.py"
        # Replayable seeds vary whether the first result receives its API receipt
        # while another interpreter request waits for headers or ignores cleanup.
        for seed in (14, 41, 97):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                report_path = root / "verdict.json"
                fixture_process = subprocess.Popen(
                    (sys.executable, "-I", "-B", str(fixture), "scenario", str(root / "http-fault.jsonl"),
                     str(report_path), str(seed)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True, env={**os.environ, "TMPDIR": str(root)},
                )
                report = None
                try:
                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        if report_path.exists():
                            report = json.loads(report_path.read_text())
                            break
                        if fixture_process.poll() is not None:
                            break
                        time.sleep(0.01)
                finally:
                    for number, wait_seconds in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 2)):
                        try:
                            os.killpg(fixture_process.pid, number)
                        # silent-defaults: allow-contract -- A missing disposable group is already stopped; communicate still reaps its original process.
                        except ProcessLookupError:
                            pass
                        try:
                            fixture_process.wait(timeout=wait_seconds)
                        # silent-defaults: allow-contract -- Only outer-fixture teardown escalates after its own bounded wait, after the recorded verdict.
                        except subprocess.TimeoutExpired:
                            pass
                    stdout, stderr = fixture_process.communicate(timeout=2)

                self.assertIsNotNone(report, msg="No pre-teardown recovery verdict:\n" + stdout + stderr)
                evidence = json.dumps(report, sort_keys=True)
                self.assertNotIn("scenario_failure", report, msg=evidence)
                self.assertEqual(5, report["repetitions"], msg=evidence)
                self.assertGreater(report["repetitions"], report["maximum_attempts"], msg=evidence)
                self.assertEqual(5, len(report["cycles"]), msg=evidence)
                for cycle in report["cycles"]:
                    self.assertEqual(report["provider_pid"], cycle["provider_pid"], msg=evidence)
                    self.assertFalse(cycle["http_child_alive"], msg=evidence)
                    # Before expecting TERM/KILL escalation, prove that the
                    # original request actually reached the cancellable read
                    # and the intentionally noncooperative cleanup. An early
                    # SIGUSR1 during child startup is a different failure.
                    self.assertTrue(
                        {"read_cancelled", "cleanup_engaged", "term_ignored"} <= set(cycle["fault_events"]),
                        msg="Fault was not engaged before HTTP child exit; "
                            "inspect the original model startup deadline: " + evidence,
                    )
                    self.assertEqual([signal.SIGUSR1, signal.SIGTERM, signal.SIGKILL], cycle["signals_sent"], msg=evidence)
                    self.assertEqual(-signal.SIGKILL, cycle["http_child_returncode"], msg=evidence)
                    self.assertEqual(0, cycle["open_prose_connections"], msg=evidence)
                    self.assertEqual(0, cycle["live_claims"], msg=evidence)
                    self.assertTrue(cycle["controller_shutdown_ready"], msg=evidence)
                    self.assertTrue(cycle["delivery_worker_running"], msg=evidence)
                    expected_descriptors = report["idle_descriptors"] if cycle["receipt_released"] else report["pending_delivery_descriptors"]
                    self.assertEqual(expected_descriptors, cycle["descriptors"], msg=evidence)
                    if not cycle["receipt_released"]:
                        self.assertTrue(cycle["original_result_pending"], msg=evidence)
                        self.assertTrue(cycle["delivery_original_request_owned"], msg=evidence)
                    for fact in ("first_cause_is_timeout", "original_first_cause_retained",
                                 "original_interpreter_failure_retained", "timeout_in_failure_graph", "timeout_in_diagnostic"):
                        self.assertTrue(cycle[fact], msg=fact + ": " + evidence)
                    self.assertEqual(report["scientific_worker_before"]["pid"], cycle["scientific_worker"]["pid"], msg=evidence)
                    self.assertEqual(1, cycle["scientific_worker"]["model_loads"], msg=evidence)
                    self.assertEqual(1, cycle["scientific_worker"]["executions"], msg=evidence)
                self.assertTrue(any(not cycle["receipt_released"] for cycle in report["cycles"]), msg=evidence)
                self.assertTrue(any(cycle["receipt_released"] for cycle in report["cycles"][:-1]), msg=evidence)
                self.assertEqual(report["acknowledgement_cycle"], report["acknowledgement_released_during_cycle"], msg=evidence)
                self.assertEqual(1, report["live_claims_at_acknowledgement_release"], msg=evidence)
                self.assertTrue(report["original_api_request_owned_at_acknowledgement_release"], msg=evidence)
                self.assertEqual(1, report["open_prose_connections_at_acknowledgement_release"], msg=evidence)
                child_pids = {cycle["http_child_pid"] for cycle in report["cycles"]}
                self.assertEqual(5, len(child_pids), msg=evidence)
                self.assertFalse(child_pids & {report["provider_pid"], report["scientific_worker_before"]["pid"]}, msg=evidence)
                self.assertEqual(["/ready", *(["/hang"] * 5), "/ready"], report["prose_request_paths"], msg=evidence)
                self.assertTrue(report["original_result_bytes_sent_once"], msg=evidence)
                self.assertEqual(report["original_result_acceptance"]["execution_attempt_ref"],
                                 report["original_result_receipt_reference"], msg=evidence)
                self.assertEqual(report["original_result_receipt_result"], report["original_result_receipt_verified"], msg=evidence)
                self.assertTrue(report["delivery_request_bodies_match"], msg=evidence)
                self.assertEqual(7, len(report["published_attempts"]), msg=evidence)
                self.assertEqual(["delivery_published"] * 7, [facts["event"] for _, facts in report["notices"]], msg=evidence)
                self.assertEqual([], report["queue_inventory"], msg=evidence)
                self.assertEqual(0, report["final_live_claims"], msg=evidence)
                self.assertEqual(0, report["final_open_prose_connections"], msg=evidence)
                self.assertEqual(0, report["final_open_jobs_api_connections"], msg=evidence)
                self.assertEqual(report["idle_descriptors"], report["final_descriptors"], msg=evidence)
                for job in report["jobs"]:
                    expected_science = 1
                    if job["name"].startswith("timeout-"):
                        expected_science = 0
                    self.assertEqual((1, 1, expected_science, expected_science),
                                     (job["starts"], job["session_prepare_calls"], job["scientific_prepare_calls"], job["scientific_execute_calls"]),
                                     msg=evidence)
                    self.assertTrue(job["shutdown_ready"], msg=evidence)
                before, after = report["scientific_worker_before"], report["scientific_worker_after"]
                self.assertEqual(before["pid"], after["pid"], msg=evidence)
                self.assertEqual((1, 2, 2, 7), (after["model_loads"], after["preparations"], after["executions"], after["retirements"]), msg=evidence)
