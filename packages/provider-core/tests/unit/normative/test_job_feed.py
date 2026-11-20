"""Signed discovery yields immutable facts for the existing exact-input owner."""

import unittest
from datetime import UTC

from tests.fakes.api import Transport, reply
from tests.fakes.job_feed import response
from tests.fakes.job_input import facts
from tests.fakes.api import api


class JobFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_query_is_explicit_and_page_does_not_auto_follow_cursor(self):
        transport = Transport(reply(response()))

        jobs_page = await api(transport, "https://api.example").jobs(
            analysis_kind_ref="nmr_predict",
            has_provider_execution_attempt=False,
            limit=2,
            cursor="Yg"
        )

        self.assertEqual(len(transport.calls), 1)
        (signed_request, bound) = transport.calls[0]

        self.assertEqual(signed_request.method, "GET")
        self.assertEqual(signed_request.url.raw_path, b"/provider/v1/jobs?analysis_kind_ref=nmr_predict&has_provider_execution_attempt=false&limit=2&cursor=Yg")
        self.assertEqual(bound, 65536)
        self.assertEqual(signed_request.content, b"")
        self.assertNotIn("Content-Digest", signed_request.headers)
        self.assertNotIn("Content-Type", signed_request.headers)
        self.assertIn("Signature", signed_request.headers)
        self.assertIsInstance(jobs_page.jobs, tuple)
        self.assertEqual(jobs_page.jobs[0].input, facts())
        self.assertEqual(jobs_page.jobs[0].created_at.tzinfo, UTC)
        self.assertEqual(jobs_page.next_cursor, "YQ")
