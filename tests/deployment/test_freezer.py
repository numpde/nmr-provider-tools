"""The installed freezer grants only delivery inputs and retains existing state."""

from provider_core.container_guard import require_container

require_container()

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fakes.credential import document, encoded
from tests.unit.normative.test_deployment_binding import definition

specification = importlib.util.spec_from_file_location("delivery_deployment", "/opt/provider-tools/delivery_deployment.py")
freezer = importlib.util.module_from_spec(specification)
specification.loader.exec_module(freezer)


class InstalledFreezerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "bundles/generation-fixture"
        self.output.mkdir(mode=0o700, parents=True)
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.binding_file = self.root / "definition.toml"
        self.raw = definition(1).replace(b"uid = 65532", f"uid = {self.uid}".encode()).replace(b"gid = 65532", f"gid = {self.gid}".encode())
        self.credential = self.root / "signing.json"
        self.credential.write_bytes(encoded(document(principal_ref="provider:fixture-0")))
        self.credential.chmod(0o600)
        self.image = "sha256:" + "a" * 64

    def write_definition(self, raw):
        self.binding_file.write_bytes(raw)
        self.binding_file.chmod(0o600)

    def read_grant(self, value, **limits):
        if str(value) == "/synthetic/signing.json":
            return freezer.read_startup_file(self.credential, **limits)
        if str(value) == "/synthetic/resolver":
            return b"nameserver 127.0.0.1\n"
        raise AssertionError("The freezer requested an unselected input grant")

    def test_long_derived_daemon_socket_refuses_before_output_or_secret_read(self):
        self.write_definition(self.raw.replace(b"delivery.sock", b"s" * 40 + b".sock"))
        with patch.object(freezer, "_read_grant") as read:
            with self.assertRaisesRegex(freezer.DeliveryDeploymentRejected, "107-byte"):
                freezer.prepare(self.binding_file, self.output, self.image)
        read.assert_not_called()
        self.assertEqual([], list(self.output.iterdir()))
        result = subprocess.run([sys.executable, "-I", "-B", "/opt/provider-tools/delivery_deployment.py",
                                 "inspect-definition", "--definition", str(self.binding_file)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertIn("107-byte", result.stderr)

    def test_mapped_route_has_exact_runtime_recipients_and_no_scientific_control(self):
        self.write_definition(self.raw)
        with patch.object(freezer, "_read_grant", side_effect=self.read_grant) as read:
            freezer.prepare(self.binding_file, self.output, self.image)
        self.assertEqual(["/synthetic/signing.json"], [str(call.args[0]) for call in read.call_args_list])
        plan = json.loads((self.output / "compose.json").read_text())
        daemon, initializer = plan["services"]["delivery"], plan["services"]["init"]
        self.assertEqual("none", daemon["network_mode"])
        self.assertEqual(["CHOWN"], initializer["cap_add"])
        self.assertNotIn("--control", initializer["command"])
        self.assertEqual(2, len(initializer["volumes"]))
        self.assertTrue(all("-control-" not in name for name in plan["volumes"]))
        proxy = next(service for name, service in plan["services"].items() if name.startswith("api-"))
        self.assertEqual("host", proxy["network_mode"])
        self.assertEqual("INFO", proxy["command"][proxy["command"].index("--log-level") + 1])
        self.assertEqual(["/route"], [mount["target"] for mount in proxy["volumes"]])
        self.assertTrue(all(mount["type"] == "volume" for mount in initializer["volumes"]))
        config = tomllib.loads((self.output / "daemon.toml").read_text())
        api = config["bindings"][0]["api"]
        self.assertTrue(any(mount["target"] == api["credential_file"] and mount["read_only"] for mount in daemon["volumes"]))
        self.assertEqual(str(self.root), daemon["labels"]["org.nmr-provider-tools.root"])

    def test_local_network_uses_immutable_identity_and_external_resolver_only_reaches_proxy(self):
        for mode in ("local", "external"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.root) as directory:
                output = Path(directory)
                output.chmod(0o700)
                if mode == "local":
                    raw = self.raw.replace(b'mode = "mapped"', b'mode = "local"\nnetwork_id = "' + b"b" * 64 + b'"')
                else:
                    raw = self.raw.replace(b'mode = "mapped"', b'mode = "external"').replace(b'addresses = ["api.example=127.0.0.1"]', b'resolver_file = "/synthetic/resolver"')
                self.write_definition(raw)
                with patch.object(freezer, "_read_grant", side_effect=self.read_grant):
                    freezer.prepare(self.binding_file, output, self.image)
                plan = json.loads((output / "compose.json").read_text())
                proxy = next(service for name, service in plan["services"].items() if name.startswith("api-"))
                if mode == "local":
                    self.assertNotIn("network_mode", proxy)
                    self.assertEqual([{"external": True, "name": "b" * 64}], list(plan["networks"].values()))
                else:
                    self.assertIn("/etc/resolv.conf", [mount["target"] for mount in proxy["volumes"]])
                    self.assertTrue(all(not mount["source"].endswith("resolv.conf") for mount in plan["services"]["delivery"]["volumes"]))

    def test_initializer_preserves_correct_nonempty_roots_and_refuses_reownership(self):
        queue, ipc = self.root / "queue", self.root / "ipc"
        queue.mkdir(mode=0o700)
        ipc.mkdir(mode=0o710)
        (queue / "retained").write_bytes(b"obligation")
        (ipc / "retained").write_bytes(b"socket ownership marker")
        with patch.object(freezer.os, "geteuid", return_value=0), patch.object(freezer.os, "fchown") as chown:
            freezer.initialize(self.uid, self.gid, queue, [ipc])
        chown.assert_not_called()
        self.assertEqual(b"obligation", (queue / "retained").read_bytes())
        ipc.chmod(0o700)
        with patch.object(freezer.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(freezer.DeliveryDeploymentRejected, "Refusing to change"):
                freezer.initialize(self.uid, self.gid, queue, [ipc])
        self.assertEqual(b"socket ownership marker", (ipc / "retained").read_bytes())

    def test_cli_help_keeps_argparse_success_and_binding_refusal_keeps_safe_reason(self):
        command = [sys.executable, "-I", "-B", "/opt/provider-tools/delivery_deployment.py"]
        result = subprocess.run([*command, "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.write_definition(self.raw.replace(b'interface = "lo"', b'interface = 42'))
        result = subprocess.run([*command, "inspect-definition", "--definition", str(self.binding_file)], capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertIn("interface must be", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
