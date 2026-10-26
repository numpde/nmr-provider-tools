"""Signed discovery yields immutable facts for the existing exact-input owner."""

import json
import unittest
from datetime import UTC

from provider_core.job_feed import PAGE_RESPONSE_BYTES, read_jobs_page
from provider_core.response_json import ResponseRejected
from tests.fakes.api import Transport, reply
from tests.fakes.job_feed import page, response
from tests.fakes.job_input import facts
from tests.fakes.api import api


class JobFeedTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_signed_query_is_explicit_and_page_does_not_auto_follow_cursor(self):
        transport = Transport(reply(response()))
        result = await api(transport, "https://api.example").jobs(analysis_kind_ref="nmr_predict",
                                                              has_provider_execution_attempt=False, limit=2, cursor="Yg")
        self.assertEqual(len(transport.calls), 1)
        request, bound = transport.calls[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.raw_path, b"/provider/v1/jobs?analysis_kind_ref=nmr_predict&has_provider_execution_attempt=false&limit=2&cursor=Yg")
        self.assertEqual(bound, 65536)
        self.assertEqual(request.content, b"")
        self.assertNotIn("Content-Digest", request.headers)
        self.assertNotIn("Content-Type", request.headers)
        self.assertIn("Signature", request.headers)
        self.assertIsInstance(result.jobs, tuple)
        self.assertEqual(result.jobs[0].input, facts())
        self.assertEqual(result.jobs[0].created_at.tzinfo, UTC)
        self.assertEqual(result.next_cursor, "YQ")

    def test_empty_page_and_presence_view_are_retained(self):
        document = page()
        document.update(jobs=[], next_cursor=None, has_provider_execution_attempt=True)
        result = read_jobs_page(json.dumps(document).encode(), analysis_kind_ref="nmr_predict",
                                has_provider_execution_attempt=True, limit=1)
        self.assertEqual(result.jobs, ())
        self.assertIsNone(result.next_cursor)
        self.assertTrue(result.has_provider_execution_attempt)
