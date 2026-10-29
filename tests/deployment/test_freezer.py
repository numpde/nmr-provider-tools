"""Installed deployment rendering grants inputs and keeps managed state in Docker."""

from provider_core.container_guard import require_container

require_container()

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.unit.normative.test_deployment_binding import definition

specification = importlib.util.spec_from_file_location("delivery_deployment", "/opt/provider-tools/delivery_deployment.py")
freezer = importlib.util.module_from_spec(specification)
specification.loader.exec_module(freezer)


class InstalledDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.binding_file = self.root / "definition.toml"
        self.raw = definition(1).replace(b"uid = 65532", f"uid = {self.uid}".encode()).replace(b"gid = 65532", f"gid = {self.gid}".encode())
        self.image = "sha256:" + "a" * 64

    def write_definition(self, raw):
        self.binding_file.write_bytes(raw)
        self.binding_file.chmod(0o600)

    def render(self, raw):
        self.write_definition(raw)
        selected, checksum = freezer.read_definition(self.binding_file)
        return freezer.render(selected, self.binding_file, self.image, checksum)

    def test_long_derived_daemon_socket_refuses_before_recipe_or_secret_reads(self):
        self.write_definition(self.raw.replace(b"delivery.sock", b"s" * 40 + b".sock"))
        with patch.object(freezer, "read_startup_file", wraps=freezer.read_startup_file) as read:
            with self.assertRaisesRegex(freezer.DeliveryDeploymentRejected, "107-byte"):
                freezer.read_definition(self.binding_file)
        self.assertEqual([self.binding_file], [call.args[0] for call in read.call_args_list])
        self.assertEqual([self.binding_file], list(self.root.iterdir()))

    def test_render_reads_no_credentials_and_grants_only_config_files_and_named_runtime_volumes(self):
        self.write_definition(self.raw)
        with patch.object(freezer, "read_startup_file", wraps=freezer.read_startup_file) as read:
            selected, checksum = freezer.read_definition(self.binding_file)
            plan = freezer.render(selected, self.binding_file, self.image, checksum)
        self.assertEqual([self.binding_file], [call.args[0] for call in read.call_args_list])
        self.assertEqual([self.binding_file], list(self.root.iterdir()))
        daemon, initializer = plan["services"]["delivery"], plan["services"]["init"]
        self.assertEqual("none", daemon["network_mode"])
        self.assertEqual(["CHOWN"], initializer["cap_add"])
        self.assertEqual(2, len(initializer["volumes"]))
        self.assertNotIn("--control", initializer["command"])
        self.assertTrue(all("-control-" not in name for name in plan["volumes"]))
        proxy = next(service for name, service in plan["services"].items() if name.startswith("api-") and not name.startswith("api-init-"))
        self.assertEqual("host", proxy["network_mode"])
        self.assertEqual("INFO", proxy["command"][proxy["command"].index("--log-level") + 1])
        self.assertEqual(["/route"], [mount["target"] for mount in proxy["volumes"]])
        route_init_name = next(name for name in plan["services"] if name.startswith("api-init-"))
        self.assertEqual({route_init_name: {"condition": "service_completed_successfully"}}, proxy["depends_on"])
        route_initializer = plan["services"][route_init_name]
        self.assertEqual("0:0", route_initializer["user"])
        self.assertEqual(["CHOWN"], route_initializer["cap_add"])
        self.assertEqual(["/route"], [mount["target"] for mount in route_initializer["volumes"]])
        self.assertTrue(all(mount["type"] == "volume" for mount in initializer["volumes"]))
        inputs = [mount for mount in daemon["volumes"] if mount["type"] == "bind"]
        self.assertEqual({str(self.binding_file), "/synthetic/signing.json"}, {mount["source"] for mount in inputs})
        self.assertTrue(all(mount["read_only"] for mount in inputs))
        self.assertTrue(all(mount["read_only"] for mount in daemon["volumes"] if mount["target"].startswith("/routes/")))
        self.assertEqual(checksum, daemon["command"][-1])
        projected = freezer.daemon_configuration(selected)
        self.assertEqual("/grants/synthetic/signing.json", projected["bindings"][0]["api"]["credential_file"])

    def test_shared_ca_is_one_daemon_grant_across_independent_bindings(self):
        raw = definition(2).replace(b"uid = 65532", f"uid = {self.uid}".encode()).replace(b"gid = 65532", f"gid = {self.gid}".encode())
        raw = raw.replace(b'credential_file = "/synthetic/signing.json"', b'credential_file = "/synthetic/first.json"', 1)
        raw = raw.replace(b'credential_file = "/synthetic/signing.json"', b'credential_file = "/synthetic/second.json"', 1)
        raw = raw.replace(b"[bindings.route]", b'ca_file = "/synthetic/shared-ca.pem"\n[bindings.route]')
        plan = self.render(raw)
        mounts = plan["services"]["delivery"]["volumes"]
        self.assertEqual(1, sum(mount["source"] == "/synthetic/shared-ca.pem" for mount in mounts))
        self.assertEqual(len(mounts), len({mount["target"] for mount in mounts}))

    def test_local_network_uses_immutable_identity_and_external_resolver_only_reaches_proxy(self):
        for mode in ("local", "external"):
            with self.subTest(mode=mode):
                if mode == "local":
                    raw = self.raw.replace(b'mode = "mapped"', b'mode = "local"\nnetwork_id = "' + b"b" * 64 + b'"')
                    raw = raw.replace(b"api.example=127.0.0.1", b"api.example=172.18.0.5")
                else:
                    raw = self.raw.replace(b'mode = "mapped"', b'mode = "external"').replace(b'addresses = ["api.example=127.0.0.1"]', b'resolver_file = "/synthetic/resolver"')
                plan = self.render(raw)
                proxy = next(service for name, service in plan["services"].items() if name.startswith("api-") and not name.startswith("api-init-"))
                if mode == "local":
                    self.assertNotIn("network_mode", proxy)
                    self.assertEqual([{"external": True, "name": "b" * 64}], list(plan["networks"].values()))
                else:
                    self.assertIn("/etc/resolv.conf", [mount["target"] for mount in proxy["volumes"]])
                    self.assertTrue(all(mount["source"] != "/synthetic/resolver" for mount in plan["services"]["delivery"]["volumes"]))

    def test_initializer_preserves_correct_nonempty_queue_and_ipc_roots(self):
        queue, ipc = self.root / "queue", self.root / "ipc"
        for path, mode in ((queue, 0o700), (ipc, 0o710)):
            path.mkdir(mode=mode)
            (path / "retained").write_bytes(b"retained state")
        with patch.object(freezer.os, "geteuid", return_value=0), patch.object(freezer.os, "fchown") as chown:
            freezer.initialize(self.uid, self.gid, queue, [ipc])
        chown.assert_not_called()
        ipc.chmod(0o700)
        with patch.object(freezer.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(freezer.DeliveryDeploymentRejected, "Refusing to change"):
                freezer.initialize(self.uid, self.gid, queue, [ipc])
        for path in (queue, ipc):
            self.assertEqual(b"retained state", (path / "retained").read_bytes())

    def test_runtime_refuses_changed_definition_before_materializing_sdk_input(self):
        self.write_definition(self.raw)
        expected = hashlib.sha256(self.raw).hexdigest()
        self.write_definition(self.raw.replace(b"request_seconds = 1", b"request_seconds = 2"))
        with self.assertRaisesRegex(freezer.DeliveryDeploymentRejected, "changed after rendering"):
            freezer.runtime_configuration(self.binding_file, expected)

    def test_cli_help_and_render_keep_safe_owned_refusal(self):
        command = [sys.executable, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"]
        result = subprocess.run([*command, "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.write_definition(self.raw.replace(b'interface = "lo"', b'interface = 42'))
        result = subprocess.run([*command, "render", "--definition", str(self.binding_file), "--source", str(self.binding_file), "--image", self.image], capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertIn("interface must be", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual("", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
