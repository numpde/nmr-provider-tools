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
        (request, bound) = transport.calls[0]

        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.raw_path, b"/provider/v1/execution-attempts?state=in_progress&limit=2&cursor=Yg")
        self.assertEqual(bound, 65536)
        self.assertEqual(request.content, b"")
        self.assertNotIn("Content-Digest", request.headers)
        self.assertNotIn("Content-Type", request.headers)
        self.assertIn("Signature", request.headers)
        (item,) = attempt_page.attempts

        self.assertEqual(item.provider_attempt_key, "retained-start:01")
        self.assertEqual(item.state, "in_progress")
        self.assertEqual(item.started_at.tzinfo, UTC)
        self.assertEqual(attempt_page.next_cursor, "YQ")

        with self.assertRaises(FrozenInstanceError):
            item.job_ref = "job:replacement"
