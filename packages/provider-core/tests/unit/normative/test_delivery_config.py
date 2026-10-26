"""Startup creates explicit local owners without opening listeners or storage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from provider_core.delivery_config import load_delivery_service
from tests.fakes.delivery_config import DeliveryConfigFiles
from tests.fakes.delivery import healthy_diagnostics


class DeliveryConfigurationTests(unittest.TestCase):
    def test_original_api_grant_and_explicit_proxy_create_only_unstarted_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            files = DeliveryConfigFiles(directory)
            service = load_delivery_service(files.path, on_notice=Mock(), diagnostic_status=healthy_diagnostics)
            binding, = service.bindings
            self.assertEqual(binding.endpoint.api_origin, "https://api.example")
            self.assertEqual(binding.endpoint.provider_ref, "provider:example")
            self.assertEqual(binding.api.identity.credential_ref, "credential:example")
            self.assertEqual(binding.api.expected_topology, "dev-local")
            self.assertIsNone(service.queue)
            self.assertFalse((Path(directory) / "state").exists())
            self.assertFalse(binding.endpoint.socket_path.exists())
