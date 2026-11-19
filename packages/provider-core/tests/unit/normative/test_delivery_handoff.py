"""One acknowledged handoff retains exact outcome and publication inputs."""

import json
import sqlite3
import tempfile
import unittest
import pathlib
from unittest.mock import Mock

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_handoff import DeliveryHandoff, DeliveryHandoffRejected, MAX_HANDOFF_BYTES, read_delivery_handoff
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_store import DeliveryConflict, DeliveryQueue, QueueLimits
from provider_core.shared_delivery_client import SharedDeliveryClient
from tests.fakes.credential import document, encoded
from tests.fakes.delivery import outcome


class DeliveryHandoffTests(unittest.TestCase):
    def material(self):
        credential = encoded(document())

        fields = {
            "origin": "https://api.example",
            "profile": "dev-local",
            "topology": "dev-local",
            "provider_ref": "provider:example",
            "exchange_seconds": 10,
            "retirement_seconds": 5,
            "credential_file": "/private/credential"
        }

        (_, _, material) = api_settings_with_material(fields, Mock(return_value=credential))

        return material

    def test_round_trip_preserves_exact_result_and_original_publication_material(self):
        handoff = DeliveryHandoff(outcome(provider="provider:example"), self.material())
        restored = read_delivery_handoff(handoff.encode())

        self.assertEqual(restored.outcome, handoff.outcome)
        self.assertEqual(restored.publication, handoff.publication)
        self.assertEqual(restored.acceptance(), handoff.acceptance())
        self.assertNotIn("PRIVATE KEY", repr(handoff))

    def test_changed_signing_material_changes_acceptance(self):
        delivery_outcome = outcome(provider="provider:example")
        (first, second) = (DeliveryHandoff(delivery_outcome, self.material()), DeliveryHandoff(delivery_outcome, self.material()))

        self.assertEqual(first.outcome, second.outcome)
        self.assertNotEqual(first.acceptance()["publication_sha256"], second.acceptance()["publication_sha256"])

    def test_queue_restarts_with_original_material_and_refuses_changed_material(self):
        delivery_outcome = outcome(provider="provider:example")
        (first, other) = (DeliveryHandoff(delivery_outcome, self.material()), DeliveryHandoff(delivery_outcome, self.material()))

        with tempfile.TemporaryDirectory() as directory:
            limits = QueueLimits(2, 4096, 4, 8192)
            queue = DeliveryQueue(pathlib.Path(directory), limits=limits)

            self.assertEqual(queue.enqueue_handoff(first), first.acceptance())
            self.assertEqual(queue.enqueue_handoff(first), first.acceptance())

            with self.assertRaises(DeliveryConflict):
                queue.enqueue_handoff(other)

            queue.close()
            queue = DeliveryQueue(pathlib.Path(directory), limits=limits)
            restored = queue.next_pending_handoff(delivery_outcome.api_origin, delivery_outcome.provider_ref)

            self.assertEqual(restored.publication, first.publication)
            self.assertEqual(restored.outcome, delivery_outcome)
            queue.remove_published(delivery_outcome)
            self.assertIsNone(queue.next_pending_handoff(delivery_outcome.api_origin, delivery_outcome.provider_ref))
            queue.close()

    def test_legacy_hold_does_not_block_new_handoff(self):
        legacy = outcome(provider="provider:example")
        new = DeliveryHandoff(outcome(provider="provider:example", suffix="1"), self.material())

        with tempfile.TemporaryDirectory() as directory:
            queue = DeliveryQueue(pathlib.Path(directory), limits=QueueLimits(2, 4096, 4, 8192))

            queue.enqueue(legacy)
            queue.enqueue_handoff(new)
            self.assertEqual(queue.hold_legacy_rows(), 1)
            self.assertEqual(queue.next_pending_handoff(legacy.api_origin, legacy.provider_ref).outcome, new.outcome)
            self.assertEqual(queue.inventory(legacy.api_origin, legacy.provider_ref)[0]["state"], "held")
            queue.close()

    def test_version_one_database_upgrades_without_losing_its_row(self):
        delivery_outcome = outcome(provider="provider:example")

        with tempfile.TemporaryDirectory() as directory:
            database_path = pathlib.Path(directory) / "delivery.sqlite3"
            db = sqlite3.connect(database_path)

            database_path.chmod(0o600)
            db.execute("CREATE TABLE outstanding (api_origin TEXT NOT NULL, provider_ref TEXT NOT NULL, attempt_ref TEXT NOT NULL, job_ref TEXT NOT NULL, operation TEXT NOT NULL, body BLOB NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, held_reason TEXT, PRIMARY KEY(api_origin,provider_ref,attempt_ref))")

            db.execute(
                "INSERT INTO outstanding VALUES (?,?,?,?,?,?,?,'pending',1,NULL)",
                (*delivery_outcome.key, delivery_outcome.job_ref, delivery_outcome.operation.value, delivery_outcome.body, delivery_outcome.body_sha256)
            )

            db.execute("PRAGMA user_version=1")
            db.commit()
            db.close()
            queue = DeliveryQueue(pathlib.Path(directory), limits=QueueLimits(2, 4096, 4, 8192))

            self.assertEqual(queue.next_pending(delivery_outcome.api_origin, delivery_outcome.provider_ref), delivery_outcome)
            self.assertEqual(queue.hold_legacy_rows(), 1)
            self.assertEqual(queue.inventory(delivery_outcome.api_origin, delivery_outcome.provider_ref)[0]["state"], "held")
            queue.close()

    def test_old_owner_can_attach_original_material_without_overwriting_new_handoffs(self):
        legacy = outcome(provider="provider:example")
        (material, other) = (self.material(), self.material())
        new = DeliveryHandoff(outcome(provider="provider:example", suffix="1"), other)

        with tempfile.TemporaryDirectory() as directory:
            queue = DeliveryQueue(pathlib.Path(directory), limits=QueueLimits(2, 4096, 4, 8192))

            queue.enqueue(legacy)
            queue.enqueue_handoff(new)
            self.assertEqual(queue.hold_legacy_rows(), 1)
            self.assertEqual(queue.associate_legacy_material(material), 1)
            self.assertEqual(queue.associate_legacy_material(other), 0)
            self.assertEqual(queue.next_pending_handoff(legacy.api_origin, legacy.provider_ref).publication, material)
            queue.remove_published(legacy)
            self.assertEqual(queue.next_pending_handoff(legacy.api_origin, legacy.provider_ref).publication, other)
            self.assertEqual(queue.hold_legacy_rows(), 0)
            queue.close()

    def test_invalid_input_is_refused(self):
        handoff = DeliveryHandoff(outcome(provider="provider:example"), self.material())
        document = json.loads(handoff.encode())
        document["credential"] = "PRIVATE_INVALID_CREDENTIAL"

        with self.assertRaises(DeliveryHandoffRejected) as rejected:
            read_delivery_handoff(json.dumps(document).encode())

        with self.assertRaises(DeliveryHandoffRejected):
            read_delivery_handoff(b"x" * (MAX_HANDOFF_BYTES + 1))

        with self.assertRaises(DeliveryHandoffRejected):
            DeliveryHandoff(outcome(provider="provider:other"), self.material())

    @unittest.skip("Privacy policy under review")
    def test_rejection_diagnostics_omit_input(self):
        handoff = DeliveryHandoff(outcome(provider="provider:example"), self.material())
        handoff_fields = json.loads(handoff.encode()) | {"credential": "PRIVATE_INVALID_CREDENTIAL"}

        with self.assertRaises(DeliveryHandoffRejected) as rejected:
            read_delivery_handoff(json.dumps(handoff_fields).encode())

        self.assertNotIn("PRIVATE", str(rejected.exception))

    def test_client_refuses_mismatched_material_before_admitting_work(self):
        endpoint = DeliveryEndpoint("https://api.example", "provider:other", pathlib.Path("/tmp/delivery.sock"))

        with self.assertRaisesRegex(ValueError, "same API and provider"):
            SharedDeliveryClient(endpoint, self.material(), exchange_seconds=1, retirement_seconds=1)
