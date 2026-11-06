"""The active Attempt's stop request retires its original HTTP child and socket."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class ProviderHttpShutdownTests(unittest.TestCase):
    def run_schedules(self, mode, seed):
        fixture = Path(__file__).resolve().parents[3] / "fakes/b3_provider_shutdown.py"
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "verdict.json"
            process = subprocess.Popen(
                (sys.executable, "-I", "-B", str(fixture), str(report_path), mode, str(seed)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                env={**os.environ, "TMPDIR": directory},
            )
            report = None
            try:
                # Each seeded schedule has explicit event barriers and a
                # bounded 0.6s HTTP cleanup allowance, not an unbounded fuzzer.
                allowance = 10
                if mode == "seeded":
                    allowance = 45
                deadline = time.monotonic() + allowance
                while time.monotonic() < deadline:
                    if report_path.exists():
                        report = json.loads(report_path.read_text())
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
            finally:
                # The verdict has already sampled actual SDK cleanup. Killing
                # this disposable group afterwards cannot satisfy the assertions.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                # silent-defaults: allow-contract -- An already exited process has no remaining process group to terminate; cleanup continues to reap.
                # silent-defaults: allow-contract -- A process reaped before escalation cannot receive SIGKILL; the caller still collects shutdown evidence.
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=0.5)
                # silent-defaults: allow-contract -- A child that outlives the grace period is escalated to SIGKILL; no capacity is released early.
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate(timeout=2)
            self.assertIsNotNone(report, msg="No bounded shutdown verdict was produced:\n" + stdout + stderr)
            self.assertNotIn("fixture_failure", report, msg=json.dumps(report, sort_keys=True))
            return report["scenarios"]

    def assert_original_http_child_retired(self, facts):
        evidence = json.dumps(facts, sort_keys=True)
        for flag in ("shutdown_ready", "work_settled", "session_retired", "http_retirement_confirmed",
                     "original_driver_retained", "request_deadline_unchanged", "stop_time_unchanged"):
            self.assertTrue(facts[flag], msg=evidence)
        self.assertIn("ExecutionStopRequested", facts["controller_failure_types"], msg=evidence)
        self.assertEqual(1, facts["prepare_calls"], msg=evidence)
        self.assertEqual(0, facts["execute_calls"], msg=evidence)
        self.assertEqual(0, facts["tool_dispatches"], msg=evidence)
        self.assertEqual(1, facts["start_count"], msg=evidence)
        self.assertEqual(1, facts["child_count"], msg=evidence)
        self.assertTrue(all(code is not None for code in facts["child_returncodes"]), msg=evidence)
        self.assertEqual([False], facts["child_still_alive"], msg=evidence)
        self.assertEqual([True], facts["child_stdin_closed"], msg=evidence)
        self.assertEqual(1, facts["exit_observer_count"], msg=evidence)
        self.assertEqual(1, len(facts["child_exit_seconds_after_stop"]), msg=evidence)
        self.assertFalse(facts["exit_observer_cancelled"], msg=evidence)
        self.assertEqual(0, facts["unfinished_peer_connections"], msg=evidence)
        self.assertEqual(facts["descriptors_before_launch"], facts["descriptors_after_cleanup"], msg=evidence)
        self.assertFalse(facts["trust_directory_exists"], msg=evidence)
        # The provider requested local stop, not a terminal Job cancellation
        # from the Jobs API. Its passive Attempt claim remains intentionally held.
        self.assertEqual(1, facts["claim_count_after_local_stop"], msg=evidence)

        stage = facts["schedule"]["stage"]
        # Leave 150 ms for the OS to schedule and reap the signalled child.
        # A creation result delivered after the original allowance gets no
        # new grace period; retirement must proceed when that result arrives.
        exit_limit = (facts["observations_released_seconds_after_stop"] if stage == "late_startup"
                      else facts["cleanup_seconds"])
        self.assertLessEqual(facts["child_exit_seconds_after_stop"][0], exit_limit + 0.15, msg=evidence)
        expected_requests = 1
        if stage in {"startup", "late_startup"}:
            expected_requests = 0
        self.assertEqual(expected_requests, facts["http_request_count"], msg=evidence)
        if expected_requests:
            self.assertTrue(facts["socket_closed_by_child"], msg=evidence)
        if facts["schedule"]["ignore_stop_signals"]:
            for event in ("stop_request_ignored", "term_ignored"):
                self.assertIn(event, facts["http_fault_events"], msg=evidence)
            self.assertEqual([-signal.SIGKILL], facts["child_returncodes"], msg=evidence)

    def assert_unconfirmed_child_remains_owned(self, facts):
        evidence = json.dumps(facts, sort_keys=True)
        held = facts["held"]
        for flag in ("shutdown_ready", "session_retired", "http_retirement_confirmed"):
            self.assertFalse(held[flag], msg=evidence)
        for flag in ("original_owner_retained", "original_exchange_retained", "replacement_job_refused",
                     "replacement_http_request_refused", "trust_directory_exists"):
            self.assertTrue(held[flag], msg=evidence)
        self.assertEqual(1, held["claim_count"], msg=evidence)
        if facts["schedule"]["stage"] == "late_exit_observation":
            self.assertFalse(held["child_alive"], msg=evidence)
            self.assertTrue(held["actual_child_exit_observed"], msg=evidence)
        else:
            self.assertTrue(held["child_alive"], msg=evidence)
        if facts["schedule"]["stage"] in {"late_startup", "late_exit_observation"}:
            self.assertTrue(held["repeated_retirement_finished_without_new_allowance"], msg=evidence)
            self.assertIn("_ExitPending", held["retirement_failure_types"], msg=evidence)

    def test_active_attempt_stop_during_http_child_startup_retires_that_child(self):
        facts, = self.run_schedules("startup", 0)
        self.assert_unconfirmed_child_remains_owned(facts)
        self.assert_original_http_child_retired(facts)

    def test_active_attempt_stop_during_blocked_https_closes_child_and_connection(self):
        facts, = self.run_schedules("https_wait", 0)
        self.assert_original_http_child_retired(facts)

    def test_active_attempt_stop_kills_http_child_that_ignores_stop_signals(self):
        facts, = self.run_schedules("uncooperative_https", 0)
        self.assert_original_http_child_retired(facts)

    def test_late_http_child_startup_keeps_claim_while_exit_is_unconfirmed(self):
        facts, = self.run_schedules("late_startup", 0)
        self.assert_unconfirmed_child_remains_owned(facts)
        self.assert_original_http_child_retired(facts)

    def test_delayed_exit_observation_keeps_slot_and_does_not_renew_cleanup_deadline(self):
        facts, = self.run_schedules("late_exit_observation", 0)
        self.assert_unconfirmed_child_remains_owned(facts)
        self.assert_original_http_child_retired(facts)

    def test_seeded_shutdown_schedules_preserve_original_child_and_cleanup(self):
        scenarios = self.run_schedules("seeded", 24)
        self.assertEqual(24, len(scenarios))
        for facts in scenarios:
            with self.subTest(seed=facts["seed"], schedule=facts["schedule"], trace=facts["trace"]):
                if "held" in facts:
                    self.assert_unconfirmed_child_remains_owned(facts)
                self.assert_original_http_child_retired(facts)
