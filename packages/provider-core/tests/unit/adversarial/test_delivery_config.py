"""Bad configuration or credential grants refuse before acquiring runtime state."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from provider_core.delivery_config import DeliveryConfigurationRejected, load_delivery_service
from tests.fakes.delivery_config import DeliveryConfigFiles
from tests.fakes.delivery import healthy_diagnostics


class DeliveryConfigurationRefusalTests(unittest.TestCase):
    def test_closed_fields_and_credential_binding_refuse_without_private_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            files = DeliveryConfigFiles(directory)
            cases = (
                b"PRIVATE_CANARY malformed TOML",
                files.raw.replace(b"request_seconds = 1", b"PRIVATE_CANARY = 'PRIVATE_VALUE'"),
                files.raw.replace(b'provider_ref = "provider:example"', b'provider_ref = "provider:other"'),
                files.raw.replace(b'profile = "dev-local"', b'profile = "run"'),
                files.raw.replace(b'retry_initial_seconds = 0.01', b'retry_initial_seconds = nan'),
                files.raw.replace(b'request_seconds = 1', b'request_seconds = ' + b'9' * 400),
                files.raw.replace(b'proxy_socket = ', b'PRIVATE_CANARY = '),
            )
            for raw in cases:
                with self.subTest(size=len(raw)):
                    files.write(raw)
                    with self.assertRaises(DeliveryConfigurationRejected) as refused:
                        load_delivery_service(files.path, on_notice=Mock(), diagnostic_status=healthy_diagnostics)
                    self.assertNotIn("PRIVATE", str(refused.exception))
                    self.assertFalse((Path(directory) / "state").exists())

    def test_secret_grant_stays_private_without_permission_workarounds(self):
        with tempfile.TemporaryDirectory() as directory:
            files = DeliveryConfigFiles(directory)
            for mode in (0o400, 0o600):
                files.key.chmod(mode)
                load_delivery_service(files.path, on_notice=Mock(), diagnostic_status=healthy_diagnostics)
            for mode in (0o640, 0o644, 0o666):
                files.key.chmod(mode)
                with self.subTest(mode=oct(mode)), self.assertRaisesRegex(DeliveryConfigurationRejected, "0400 or 0600"):
                    load_delivery_service(files.path, on_notice=Mock(), diagnostic_status=healthy_diagnostics)
            self.assertFalse((Path(directory) / "state").exists())
