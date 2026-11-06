"""Bound aggregate prose-service traffic and retain independent science/delivery."""

import json
from pathlib import Path
import tempfile
import unittest

from tests.fakes.b4_prose_outage import run_outage


class ProseServiceOutageTests(unittest.IsolatedAsyncioTestCase):
    def assert_outage_recovered(self, result):
        evidence = json.dumps(result, sort_keys=True)
        requests, jobs = result["requests"], result["jobs"]
        affected = [job for job in jobs if job["name"].startswith("affected-")]
        post_restoration = [job for job in affected if job["started_at"] >= result["restored_at"]]
        successful = [job for job in post_restoration if not job["failed"]]
        unavailable_requests = [request for request in requests if not request["available"]]
        self.assertGreater(result["backlog"], result["maximum_attempts"], msg=evidence)
        self.assertEqual(result["backlog"], len(affected), msg=evidence)
        self.assertEqual(len(jobs), len({job["name"] for job in jobs}), msg=evidence)
        self.assertLessEqual(result["peak_claims"], result["maximum_attempts"], msg=evidence)
        self.assertLessEqual(result["peak_prose_connections"], result["maximum_attempts"], msg=evidence)
        self.assertTrue(unavailable_requests, msg=evidence)
        self.assertTrue(
            post_restoration,
            msg="No Job remained for the SDK driver to start after restoration; "
                "the test cannot establish recovery from an empty backlog: " + evidence,
        )
        self.assertTrue(
            successful,
            msg="Jobs started after service restoration but none completed successfully "
                "(check per-Job deadline, peer availability and diagnostic causes): " + evidence,
        )
        first_success = min(successful, key=lambda job: job["finished_at"])
        self.assertGreaterEqual(first_success["finished_at"], result["restored_at"], msg=evidence)
        self.assertLessEqual(result["publication_times"][first_success["attempt_ref"]] - result["restored_at"],
                             result["recovery_seconds"], msg=evidence)
        self.assertLessEqual(len(unavailable_requests), result["request_ceiling"],
                             msg="The unavailable prose service received more requests than the fixed policy permits: " + evidence)

        # One publisher owns the exact original report throughout the outage.
        release = next(event for event in result["trace"] if event["event"] == "original_jobs_api_receipt_released")
        healthy = next(event for event in result["trace"] if event["event"] == "healthy_report_published_during_outage")
        self.assertTrue(release["publisher_owns_request"], msg=evidence)
        self.assertTrue(healthy["prose_service_still_unavailable"], msg=evidence)
        self.assertLess(release["at"], healthy["at"], msg=evidence)
        self.assertLess(healthy["at"], result["restored_at"], msg=evidence)
        self.assertTrue(result["original_report_bytes_sent_once"], msg=evidence)
        self.assertEqual(result["original_report_acceptance"]["execution_attempt_ref"],
                         result["original_report_receipt"]["execution_attempt_ref"], msg=evidence)
        self.assertEqual("analysis_result:sha256:" + result["original_report_sha256"],
                         result["original_report_receipt"]["analysis_result_ref"], msg=evidence)
        self.assertTrue(result["all_report_bytes_preserved"], msg=evidence)
        self.assertEqual(len(jobs), result["submitted_reports"], msg=evidence)
        self.assertEqual(len(jobs), result["published_reports"], msg=evidence)
        self.assertTrue(result["publisher_running"], msg=evidence)
        self.assertEqual(["delivery_published"] * len(jobs), [notice["event"] for notice in result["notices"]], msg=evidence)

        # These were sampled while peers, publisher and scientific worker still ran.
        self.assertEqual([], result["queue_inventory"], msg=evidence)
        self.assertEqual(0, result["final_claims"], msg=evidence)
        self.assertEqual([0, 0, 0], result["open_peer_connections"], msg=evidence)
        self.assertTrue(result["all_http_owners_retired"], msg=evidence)
        self.assertEqual(result["idle_descriptors"], result["final_descriptors"], msg=evidence)
        self.assertTrue(result["children"], msg=evidence)
        self.assertTrue(all(child["returncode"] is not None and not child["alive"] for child in result["children"]), msg=evidence)
        self.assertEqual(len(result["children"]), len({child["pid"] for child in result["children"]}), msg=evidence)
        before, after = result["scientific_worker_before"], result["scientific_worker_after"]
        self.assertEqual(before["pid"], after["pid"], msg=evidence)
        self.assertNotEqual(result["provider_pid"], after["pid"], msg=evidence)
        self.assertEqual(1, after["model_loads"], msg=evidence)
        executed = sum(not job["failed"] for job in jobs)
        self.assertEqual((executed, executed, len(jobs)),
                         (after["preparations"], after["executions"], after["retirements"]), msg=evidence)
        for job in jobs:
            science_count = 0 if job["failed"] else 1
            self.assertEqual((1, 1, science_count, science_count),
                             (job["starts"], job["prepare_calls"], job["scientific_prepare_calls"], job["execute_calls"]), msg=evidence)
            self.assertTrue(job["shutdown_ready"], msg=evidence)
            if job["failed"]:
                self.assertIsNotNone(job["diagnostic"], msg=evidence)
                self.assertNotIn("B4_TEST_CREDENTIAL", json.dumps(job["diagnostic"]), msg=evidence)

    async def test_retry_after_ceiling_does_not_grow_with_tenfold_backlog(self):
        for backlog in (12, 120):
            with self.subTest(backlog=backlog), tempfile.TemporaryDirectory() as directory:
                result = await run_outage(Path(directory) / "outbox", mode="retry_after", backlog=backlog, seed=17)
                self.assert_outage_recovered(result)
                evidence = json.dumps(result, sort_keys=True)
                # Each unavailable reply requests another whole second. The last
                # such response still binds the first request after restoration.
                for previous, following in zip(result["requests"], result["requests"][1:]):
                    if not previous["available"]:
                        self.assertGreaterEqual(following["at"] - previous["at"], 1, msg=evidence)

    async def test_seeded_outage_modes_preserve_original_publication_and_warm_science(self):
        for mode, seed in (("unavailable", 14), ("invalid_retry_after", 41), ("disconnect", 97), ("blocked", 23)):
            with self.subTest(mode=mode, seed=seed), tempfile.TemporaryDirectory() as directory:
                result = await run_outage(Path(directory) / "outbox", mode=mode, backlog=12, seed=seed)
                self.assert_outage_recovered(result)
                if mode == "blocked":
                    diagnostics = json.dumps([job["diagnostic"] for job in result["jobs"] if job["failed"]])
                    self.assertIn('"exception_type": "TimeoutError"', diagnostics)
