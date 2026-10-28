"""The host and providers derive grants from one neutral definition."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.deployment_binding import BindingDefinitionRejected, parse_deployment_bindings


def definition(bindings):
    text = '''project = "nmr-api-delivery"
uid = 65532
gid = 65532
request_seconds = 1
retry_initial_seconds = 0.01
retry_max_seconds = 0.1
[limits]
provider_items = 1
provider_bytes = 4096
total_items = 16
total_bytes = 65536
'''
    for index in range(bindings):
        text += f'''[[bindings]]
name = "fixture-{index}"
socket_path = "/run/delivery/delivery.sock"
[bindings.api]
origin = "https://api.example"
provider_ref = "provider:fixture-{index}"
profile = "dev-local"
topology = "dev-local"
credential_file = "/synthetic/signing.json"
exchange_seconds = 1
retirement_seconds = 0.2
[bindings.route]
mode = "mapped"
interface = "lo"
log_level = "INFO"
addresses = ["api.example=127.0.0.1"]
'''
    return text.encode()


class DeploymentBindingTests(unittest.TestCase):
    def test_cli_projects_only_selected_host_grants_without_opening_inputs(self):
        selected = parse_deployment_bindings(definition(2)).selected("fixture-1")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "definition.toml"
            path.write_bytes(definition(2))
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "provider_core.deployment_binding",
                 "describe-binding", "--definition", str(path), "--binding", "fixture-1"],
                capture_output=True, text=True, check=False, timeout=5,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({
            "ipc_volume": selected.ipc_volume,
            "socket_path": "/run/delivery/delivery.sock",
            "socket_group_id": 65532,
            "input_paths": ["/synthetic/signing.json"],
        }, json.loads(result.stdout))

    def test_selected_binding_derives_wire_identity_volume_and_group_without_opening_credentials(self):
        with patch("provider_core.deployment_binding.read_startup_file") as read:
            host = parse_deployment_bindings(definition(2))
        read.assert_not_called()
        selected = host.selected("fixture-1")
        self.assertEqual("https://api.example", selected.endpoint.api_origin)
        self.assertEqual("provider:fixture-1", selected.endpoint.provider_ref)
        self.assertEqual(65532, selected.endpoint.socket_group_id)
        self.assertEqual(Path("/run/delivery/delivery.sock"), selected.endpoint.socket_path)
        self.assertNotEqual(host.selected("fixture-0").ipc_volume, selected.ipc_volume)
        self.assertTrue(selected.ipc_volume.startswith("nmr-api-delivery-ipc-"))

    def test_fanout_bound_matches_runtime_before_any_acquisition(self):
        self.assertEqual(16, len(parse_deployment_bindings(definition(16)).bindings))
        with patch("provider_core.deployment_binding.read_startup_file") as read:
            with self.assertRaisesRegex(BindingDefinitionRejected, "sixteen"):
                parse_deployment_bindings(definition(17))
        read.assert_not_called()

    def test_wrong_interface_type_is_refused_at_the_definition_boundary(self):
        for value in ("42", "[]", "true"):
            with self.subTest(value=value):
                raw = definition(1).replace(b'interface = "lo"', f"interface = {value}".encode())
                with self.assertRaisesRegex(BindingDefinitionRejected, "interface"):
                    parse_deployment_bindings(raw)

    def test_duplicate_identity_unknown_fields_and_absent_selection_refuse(self):
        with self.assertRaisesRegex(BindingDefinitionRejected, "same API/provider"):
            parse_deployment_bindings(definition(2).replace(b"provider:fixture-1", b"provider:fixture-0"))
        with self.assertRaisesRegex(BindingDefinitionRejected, "documented fields"):
            parse_deployment_bindings(b"model = 'must remain provider-owned'\n" + definition(1))
        with self.assertRaisesRegex(BindingDefinitionRejected, "absent"):
            parse_deployment_bindings(definition(1)).selected("missing")

    def test_socket_grant_requires_a_leaf_in_a_dedicated_nonroot_parent(self):
        for path in ("/", "/delivery.sock", "//delivery.sock"):
            with self.subTest(path=path):
                raw = definition(1).replace(b'"/run/delivery/delivery.sock"', ('"' + path + '"').encode())
                with self.assertRaises(BindingDefinitionRejected):
                    parse_deployment_bindings(raw)
