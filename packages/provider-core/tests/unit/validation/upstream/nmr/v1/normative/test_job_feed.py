"""Admitted Job pages preserve requested views and the exact response ceiling."""

import json
import unittest

from provider_core.response_json import ResponseRejected
from provider_core.validation.upstream.nmr.v1.job_feed import PAGE_RESPONSE_BYTES, read_jobs_page
from tests.fakes.job_feed import page


class JobFeedValidationTests(unittest.TestCase):
    def test_maximum_page_and_exact_response_byte_boundary(self):
        job_feed_fields = page()
        job_feed_fields["jobs"] = [{**job_feed_fields["jobs"][0], "job_ref": f"job:item-{index}"} for index in range(100)]
        raw = json.dumps(job_feed_fields).encode()
        padded = raw + b" " * (PAGE_RESPONSE_BYTES - len(raw))

        self.assertEqual(len(padded), PAGE_RESPONSE_BYTES)
        jobs_page = read_jobs_page(padded, analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=100)

        self.assertEqual([job.input.job_ref for job in jobs_page.jobs], [job_fields["job_ref"] for job_fields in job_feed_fields["jobs"]])

        with self.assertRaises(ResponseRejected):
            read_jobs_page(padded + b" ", analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=100)

        job_feed_fields["jobs"].append(job_feed_fields["jobs"][0])

        with self.assertRaises(ResponseRejected):
            read_jobs_page(json.dumps(job_feed_fields).encode(), analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=100)

    def test_empty_page_and_presence_view_are_retained(self):
        job_feed_fields = page()

        job_feed_fields.update(jobs=[], next_cursor=None, has_provider_execution_attempt=True)

        jobs_page = read_jobs_page(
            json.dumps(job_feed_fields).encode(),
            analysis_kind_ref="nmr_predict",
            has_provider_execution_attempt=True,
            limit=1
        )

        self.assertEqual(jobs_page.jobs, ())
        self.assertIsNone(jobs_page.next_cursor)
        self.assertTrue(jobs_page.has_provider_execution_attempt)
