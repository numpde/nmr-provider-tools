"""One acknowledged handoff retains exact outcome and publication inputs."""

import json
import unittest
from unittest.mock import Mock

from provider_core.api_config import api_settings_with_material
from provider_core.delivery_handoff import DeliveryHandoff, DeliveryHandoffRejected, MAX_HANDOFF_BYTES, read_delivery_handoff
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
