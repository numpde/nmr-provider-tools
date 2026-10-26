"""Outstanding publication bytes survive restart; accepted payloads do not."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from provider_core.delivery_store import DeliveryConflict, DeliveryFull, DeliveryQueue, DeliveryStorageFailed, QueueLimits
from tests.fakes.delivery import outcome


class DeliveryQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.limits = QueueLimits(provider_items=1, provider_bytes=4096, total_items=2, total_bytes=8192)

    def open(self):
        queue = DeliveryQueue(self.root, limits=self.limits)
        self.addCleanup(queue.close)
        return queue

    def test_restart_and_duplicate_keep_the_exact_original_body(self):
        item = outcome()
        queue = self.open()
        accepted = queue.enqueue(item)
        self.assertEqual(accepted["body_sha256"], "sha256:" + hashlib.sha256(item.body).hexdigest())
        self.assertEqual(queue.enqueue(item), accepted)
        queue.close()
        queue = self.open()
        self.assertEqual(queue.next_pending(item.api_origin, item.provider_ref), item)
        facts = queue.inventory(item.api_origin, item.provider_ref)
        self.assertEqual(len(facts), 1)
        self.assertNotIn("body", facts[0])
        self.assertEqual(facts[0]["body_bytes"], len(item.body))

    def test_publication_removes_the_row_and_deleted_database_payload(self):
        queue = self.open()
        item = outcome()
        queue.enqueue(item)
        queue.remove_published(item)
        self.assertEqual(queue.inventory(item.api_origin, item.provider_ref), [])
        self.assertIsNone(queue.next_pending(item.api_origin, item.provider_ref))
        queue.close()
        self.assertNotIn(b"SCIENTIFIC_PAYLOAD_NEEDLE", (self.root / "delivery.sqlite3").read_bytes())
        self.assertFalse((self.root / "delivery.sqlite3-journal").exists())

    def test_held_work_counts_toward_capacity_without_blocking_another_provider(self):
        queue = self.open()
        first, other = outcome(), outcome(provider="provider:other", suffix="1")
        queue.enqueue(first)
        queue.hold(first, "The API rejected this terminal outcome; inspect operator diagnostics.")
        with self.assertRaises(DeliveryFull):
            queue.enqueue(outcome(suffix="2"))
        queue.enqueue(other)
        self.assertIsNone(queue.next_pending(first.api_origin, first.provider_ref))
        self.assertEqual(queue.next_pending(other.api_origin, other.provider_ref), other)
        self.assertEqual(queue.inventory(first.api_origin, first.provider_ref)[0]["state"], "held")

    def test_exact_byte_allowance_accepts_and_overflow_refuses_before_another_insert(self):
        first = outcome()
        self.limits = QueueLimits(provider_items=2, provider_bytes=len(first.body), total_items=4, total_bytes=2 * len(first.body))
        queue = self.open()
        queue.enqueue(first)
        with self.assertRaises(DeliveryFull):
            queue.enqueue(outcome(suffix="1"))
        queue.enqueue(outcome(provider="provider:other", suffix="1"))
        with self.assertRaises(DeliveryFull):
            queue.enqueue(outcome(provider="provider:third", suffix="2"))
        self.assertEqual(len(queue.inventory(first.api_origin, first.provider_ref)), 1)

    def test_operator_discard_requires_exclusive_storage_and_exact_held_identity(self):
        self.limits = QueueLimits(provider_items=2, provider_bytes=8192, total_items=4, total_bytes=16384)
        queue = self.open()
        held = outcome(message="DISCARDED_HELD_PAYLOAD_NEEDLE")
        pending = outcome(suffix="1")
        other = outcome(provider="provider:other", suffix="2")
        for item in (held, pending, other):
            queue.enqueue(item)
        queue.hold(held, "The API refused this outcome; operator reconciliation is required.")
        queue.hold(other, "Another binding owns this held request.")
        with self.assertRaises(DeliveryStorageFailed):
            DeliveryQueue(self.root, limits=self.limits)
        for origin, provider, attempt, digest in (
            ("https://other.example", held.provider_ref, held.execution_attempt_ref, held.body_sha256),
            (held.api_origin, other.provider_ref, held.execution_attempt_ref, held.body_sha256),
            (held.api_origin, held.provider_ref, held.execution_attempt_ref, "sha256:" + "f" * 64),
            (pending.api_origin, pending.provider_ref, pending.execution_attempt_ref, pending.body_sha256),
        ):
            with self.assertRaises(DeliveryConflict):
                queue.discard_held(origin, provider, attempt, digest)
        queue.discard_held(held.api_origin, held.provider_ref, held.execution_attempt_ref, held.body_sha256)
        self.assertEqual(queue.next_pending(pending.api_origin, pending.provider_ref), pending)
        self.assertEqual(len(queue.inventory(other.api_origin, other.provider_ref)), 1)
        self.assertEqual(queue.inventory(held.api_origin, held.provider_ref)[0]["execution_attempt_ref"], pending.execution_attempt_ref)
        queue.close()
        self.assertNotIn(b"DISCARDED_HELD_PAYLOAD_NEEDLE", (self.root / "delivery.sqlite3").read_bytes())
