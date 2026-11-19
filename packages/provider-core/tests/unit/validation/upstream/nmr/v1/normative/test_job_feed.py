"""Admitted Job pages preserve requested views and the exact response ceiling."""

import json
import unittest

from provider_core.response_json import ResponseRejected
from provider_core.validation.upstream.nmr.v1.job_feed import PAGE_RESPONSE_BYTES, read_jobs_page
from tests.fakes.job_feed import page


class JobFeedValidationTests(unittest.TestCase):
    def test_maximum_page_and_exact_response_byte_boundary(self):
        document = page()
        document["jobs"] = [{**document["jobs"][0], "job_ref": f"job:item-{index}"} for index in range(100)]
        raw = json.dumps(document).encode()
        padded = raw + b" " * (PAGE_RESPONSE_BYTES - len(raw))

        self.assertEqual(len(padded), PAGE_RESPONSE_BYTES)
        result = read_jobs_page(padded, analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=100)

        self.assertEqual([item.input.job_ref for item in result.jobs], [item["job_ref"] for item in document["jobs"]])

        with self.assertRaises(ResponseRejected):
            read_jobs_page(padded + b" ", analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=100)

        document["jobs"].append(document["jobs"][0])

        with self.assertRaises(ResponseRejected):
            read_jobs_page(json.dumps(document).encode(), analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=100)

    def test_empty_page_and_presence_view_are_retained(self):
        document = page()

        document.update(jobs=[], next_cursor=None, has_provider_execution_attempt=True)

        result = read_jobs_page(
            json.dumps(document).encode(),
            analysis_kind_ref="nmr_predict",
            has_provider_execution_attempt=True,
            limit=1
        )

        self.assertEqual(result.jobs, ())
        self.assertIsNone(result.next_cursor)
        self.assertTrue(result.has_provider_execution_attempt)
