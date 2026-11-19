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

        result = await api(transport, "https://api.example").jobs(
            analysis_kind_ref="nmr_predict",
            has_provider_execution_attempt=False,
            limit=2,
            cursor="Yg"
        )

        self.assertEqual(len(transport.calls), 1)
        (request, bound) = transport.calls[0]

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
