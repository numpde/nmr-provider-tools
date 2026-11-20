"""Bad configuration or credential grants refuse before acquiring runtime state."""

import tempfile
import unittest
import os
import pathlib
from unittest.mock import Mock

from provider_core.delivery_config import DeliveryConfigurationRejected, load_delivery_service
from tests.fakes.delivery_config import DeliveryConfigFiles
from tests.fakes.delivery import healthy_diagnostics


class DeliveryConfigurationRefusalTests(unittest.TestCase):
    def _configuration_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            files = DeliveryConfigFiles(directory, socket_group_id=65532)

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
                        load_delivery_service(files.configuration_path, on_notice=Mock(), diagnostic_status=healthy_diagnostics)

                    yield (refused.exception, pathlib.Path(directory) / "state")

    def test_closed_fields_and_credential_binding_refuse_without_runtime_state(self):
        for (_refusal, state_directory) in self._configuration_refusals():
            self.assertFalse(state_directory.exists())

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_configuration_refusals_omit_input(self):
        for (refusal, _state_directory) in self._configuration_refusals():
            self.assertNotIn("PRIVATE", str(refusal))

    def test_secret_grant_stays_private_without_permission_workarounds(self):
        with tempfile.TemporaryDirectory() as directory:
            files = DeliveryConfigFiles(directory, socket_group_id=65532)

            for mode in (0o400, 0o600):
                files.key.chmod(mode)
                load_delivery_service(files.configuration_path, on_notice=Mock(), diagnostic_status=healthy_diagnostics)

            for mode in (0o640, 0o644, 0o666):
                files.key.chmod(mode)

                with self.subTest(mode=oct(mode)), self.assertRaisesRegex(DeliveryConfigurationRejected, "permissions="):
                    load_delivery_service(files.configuration_path, on_notice=Mock(), diagnostic_status=healthy_diagnostics)

            self.assertFalse((pathlib.Path(directory) / "state").exists())
