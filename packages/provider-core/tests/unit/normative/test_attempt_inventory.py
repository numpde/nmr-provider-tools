"""Inventory preserves provider-wide observations without selecting recovery work."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC

from tests.fakes.api import Transport, reply
from tests.fakes.attempt_inventory import response
from tests.fakes.api import api


class AttemptInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_query_is_fixed_view_with_explicit_paging_and_no_auto_follow(self):
        transport = Transport(reply(response()))
        attempt_page = await api(transport, "https://api.example").execution_attempts(state="in_progress", limit=2, cursor="Yg")

        self.assertEqual(len(transport.calls), 1)
        (signed_request, max_response_bytes) = transport.calls[0]

        self.assertEqual(signed_request.method, "GET")
        self.assertEqual(signed_request.url.raw_path, b"/provider/v1/execution-attempts?state=in_progress&limit=2&cursor=Yg")
        self.assertEqual(max_response_bytes, 65536)
        self.assertEqual(signed_request.content, b"")
        self.assertNotIn("Content-Digest", signed_request.headers)
        self.assertNotIn("Content-Type", signed_request.headers)
        self.assertIn("Signature", signed_request.headers)
        (attempt_record,) = attempt_page.attempts

        self.assertEqual(attempt_record.provider_attempt_key, "retained-start:01")
        self.assertEqual(attempt_record.state, "in_progress")
        self.assertEqual(attempt_record.started_at.tzinfo, UTC)
        self.assertEqual(attempt_page.next_cursor, "YQ")

        with self.assertRaises(FrozenInstanceError):
            attempt_record.job_ref = "job:replacement"
