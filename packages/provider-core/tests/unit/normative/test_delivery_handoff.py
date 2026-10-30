"""One acknowledged handoff retains exact outcome and publication inputs."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_handoff import DeliveryHandoff, DeliveryHandoffRejected, MAX_HANDOFF_BYTES, read_delivery_handoff
from provider_core.delivery_store import DeliveryConflict, DeliveryQueue, QueueLimits
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class DeliveryHandoffTests(unittest.TestCase):
    def material(self):
        credential = encoded(document())
        fields = {"origin": "https://api.example", "profile": "dev-local", "topology": "dev-local",
                  "provider_ref": "provider:example", "exchange_seconds": 10, "retirement_seconds": 5,
                  "credential_file": "/private/credential"}
        _, _, material = api_settings_with_material(fields, Mock(return_value=credential))
        return material

    def test_round_trip_preserves_exact_result_and_original_publication_material(self):
        handoff = DeliveryHandoff(outcome(provider="provider:example"), self.material())
        restored = read_delivery_handoff(handoff.encode())
        self.assertEqual(restored.outcome, handoff.outcome)
        self.assertEqual(restored.publication, handoff.publication)
        self.assertEqual(restored.acceptance(), handoff.acceptance())
        self.assertNotIn("PRIVATE KEY", repr(handoff))

    def test_changed_signing_material_changes_acceptance(self):
        item = outcome(provider="provider:example")
        first, second = DeliveryHandoff(item, self.material()), DeliveryHandoff(item, self.material())
        self.assertEqual(first.outcome, second.outcome)
        self.assertNotEqual(first.acceptance()["publication_sha256"], second.acceptance()["publication_sha256"])

    def test_queue_restarts_with_original_material_and_refuses_changed_material(self):
        item = outcome(provider="provider:example")
        first, other = DeliveryHandoff(item, self.material()), DeliveryHandoff(item, self.material())
        with tempfile.TemporaryDirectory() as directory:
            limits = QueueLimits(2, 4096, 4, 8192)
            queue = DeliveryQueue(Path(directory), limits=limits)
            self.assertEqual(queue.enqueue_handoff(first), first.acceptance())
            self.assertEqual(queue.enqueue_handoff(first), first.acceptance())
            with self.assertRaises(DeliveryConflict):
                queue.enqueue_handoff(other)
            queue.close()
            queue = DeliveryQueue(Path(directory), limits=limits)
            restored = queue.next_pending_handoff(item.api_origin, item.provider_ref)
            self.assertEqual(restored.publication, first.publication)
            self.assertEqual(restored.outcome, item)
            queue.remove_published(item)
            self.assertIsNone(queue.next_pending_handoff(item.api_origin, item.provider_ref))
            queue.close()

    def test_legacy_hold_does_not_block_new_handoff(self):
        legacy = outcome(provider="provider:example")
        new = DeliveryHandoff(outcome(provider="provider:example", suffix="1"), self.material())
        with tempfile.TemporaryDirectory() as directory:
            queue = DeliveryQueue(Path(directory), limits=QueueLimits(2, 4096, 4, 8192))
            queue.enqueue(legacy)
            queue.enqueue_handoff(new)
            self.assertEqual(queue.hold_legacy_rows(), 1)
            self.assertEqual(queue.next_pending_handoff(legacy.api_origin, legacy.provider_ref).outcome, new.outcome)
            self.assertEqual(queue.inventory(legacy.api_origin, legacy.provider_ref)[0]["state"], "held")
            queue.close()

    def test_version_one_database_upgrades_without_losing_its_row(self):
        item = outcome(provider="provider:example")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.sqlite3"
            db = sqlite3.connect(path)
            path.chmod(0o600)
            db.execute("CREATE TABLE outstanding (api_origin TEXT NOT NULL, provider_ref TEXT NOT NULL, attempt_ref TEXT NOT NULL, job_ref TEXT NOT NULL, operation TEXT NOT NULL, body BLOB NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, held_reason TEXT, PRIMARY KEY(api_origin,provider_ref,attempt_ref))")
            db.execute("INSERT INTO outstanding VALUES (?,?,?,?,?,?,?,'pending',1,NULL)",
                       (*item.key, item.job_ref, item.operation.value, item.body, item.body_sha256))
            db.execute("PRAGMA user_version=1")
            db.commit()
            db.close()
            queue = DeliveryQueue(Path(directory), limits=QueueLimits(2, 4096, 4, 8192))
            self.assertEqual(queue.next_pending(item.api_origin, item.provider_ref), item)
            self.assertEqual(queue.hold_legacy_rows(), 1)
            self.assertEqual(queue.inventory(item.api_origin, item.provider_ref)[0]["state"], "held")
            queue.close()

    def test_invalid_input_is_refused_without_rendering_private_material(self):
        handoff = DeliveryHandoff(outcome(provider="provider:example"), self.material())
        document = json.loads(handoff.encode())
        document["credential"] = "PRIVATE_INVALID_CREDENTIAL"
        with self.assertRaises(DeliveryHandoffRejected) as rejected:
            read_delivery_handoff(json.dumps(document).encode())
        self.assertNotIn("PRIVATE", str(rejected.exception))
        with self.assertRaises(DeliveryHandoffRejected):
            read_delivery_handoff(b"x" * (MAX_HANDOFF_BYTES + 1))
        with self.assertRaises(DeliveryHandoffRejected):
            DeliveryHandoff(outcome(provider="provider:other"), self.material())
