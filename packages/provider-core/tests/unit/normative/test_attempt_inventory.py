"""Inventory preserves provider-wide observations without selecting recovery work."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC

from provider_core.attempt_inventory import read_attempt_inventory
from provider_core.response_json import ResponseRejected
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_inventory import attempt, response
from tests.fakes.api import api


class AttemptInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_query_is_fixed_view_with_explicit_paging_and_no_auto_follow(self):
        transport = Transport(reply(response()))
        result = await api(transport, "https://api.example").execution_attempts(state="in_progress", limit=2, cursor="Yg")
        self.assertEqual(len(transport.calls), 1)
        request, bound = transport.calls[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.raw_path, b"/provider/v1/execution-attempts?state=in_progress&limit=2&cursor=Yg")
        self.assertEqual(bound, 65536)
        self.assertEqual(request.content, b"")
        self.assertNotIn("Content-Digest", request.headers)
        self.assertNotIn("Content-Type", request.headers)
        self.assertIn("Signature", request.headers)
        item, = result.attempts
        self.assertEqual(item.provider_attempt_key, "retained-start:01")
        self.assertEqual(item.state, "in_progress")
        self.assertEqual(item.started_at.tzinfo, UTC)
        self.assertEqual(result.next_cursor, "YQ")
        with self.assertRaises(FrozenInstanceError):
            item.job_ref = "job:replacement"

    def test_mixed_analysis_kinds_and_order_survive_as_immutable_observations(self):
        first = attempt()
        second = attempt(analysis_kind_ref="nmr_rank", job_ref="job:other",
                         execution_attempt_ref="execution_attempt:sha256:" + "b" * 64,
                         provider_attempt_key="other-start:02", started_at="2026-10-18T00:00:01Z")
        for order in ((first, second), (second, first)):
            with self.subTest(order=order[0]["job_ref"]):
                page = read_attempt_inventory(response(attempts=order), limit=2)
                self.assertIsInstance(page.attempts, tuple)
                self.assertEqual([(item.analysis_kind_ref, item.execution_attempt_ref, item.job_ref, item.provider_attempt_key)
                                  for item in page.attempts],
                                 [(item["analysis_kind_ref"], item["execution_attempt_ref"], item["job_ref"], item["provider_attempt_key"])
                                  for item in order])

    def test_empty_page_is_an_observation_without_an_absence_or_terminal_receipt(self):
        result = read_attempt_inventory(response(attempts=[], next_cursor=None), limit=1)
        self.assertEqual(result.attempts, ())
        self.assertIsNone(result.next_cursor)

    def test_maximum_items_and_exact_byte_ceiling(self):
        items = [attempt(job_ref=f"job:item-{index}", execution_attempt_ref=f"execution_attempt:sha256:{index:064x}")
                 for index in range(100)]
        raw = response(attempts=items)
        padded = raw + b" " * (65536 - len(raw))
        self.assertEqual(len(padded), 65536)
        result = read_attempt_inventory(padded, limit=100)
        self.assertEqual([item.job_ref for item in result.attempts], [item["job_ref"] for item in items])
        with self.assertRaises(ResponseRejected):
            read_attempt_inventory(padded + b" ", limit=100)
        with self.assertRaises(ResponseRejected):
            read_attempt_inventory(response(attempts=[*items, items[0]]), limit=100)

    def test_maximum_item_field_lengths_fit_within_the_route_byte_ceiling(self):
        item = attempt(analysis_kind_ref="a" * 128, job_ref="job:" + "a" * 124, provider_attempt_key="a" * 128)
        admitted = read_attempt_inventory(response(attempts=[item]), limit=1)
        self.assertEqual(admitted.attempts[0].job_ref, item["job_ref"])
        full_page = response(attempts=[item] * 100)
        self.assertLessEqual(len(full_page), 65536)
        self.assertEqual(len(read_attempt_inventory(full_page, limit=100).attempts), 100)
