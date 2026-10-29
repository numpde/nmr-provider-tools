"""Selected API routes produce private grants without reading credentials."""

import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_core.deployment_binding import parse_deployment_bindings
from provider_core.deployment_route import NamedRouteVolume, RouteDirectory, RouteProjectionRejected, initialize_route, render_api_route
from tests.unit.normative.test_deployment_binding import definition


class DeploymentRouteTests(unittest.TestCase):
    def binding(self, mode):
        raw = definition(1)
        if mode == "local":
            raw = raw.replace(b'mode = "mapped"', b'mode = "local"\nnetwork_id = "' + b"b" * 64 + b'"')
        elif mode == "external":
            raw = raw.replace(b'mode = "mapped"', b'mode = "external"')
            raw = raw.replace(b'addresses = ["api.example=127.0.0.1"]', b'resolver_file = "/synthetic/resolver"')
        return parse_deployment_bindings(raw).selected("fixture-0")

    def render(self, binding, **changes):
        arguments = {"image": "sha256:" + "a" * 64, "interpreter": "/opt/provider/bin/python",
                     "namespace": "provider-one", "uid": 65532, "gid": 65532,
                     "storage": NamedRouteVolume(), "input_sources": {}}
        arguments.update(changes)
        return render_api_route(binding, **arguments)

    def proxy(self, projection):
        role = next(iter(projection.client_depends_on))
        return projection.compose["services"][role]

    def test_actual_identity_gets_separate_private_storage_and_readonly_client_grant(self):
        binding = self.binding("mapped")
        with patch("provider_core.deployment_binding.read_startup_file") as read:
            provider = self.render(binding)
            delivery = self.render(binding, namespace="delivery-one", uid=1000, gid=1000)
        read.assert_not_called()
        self.assertEqual(provider.client_socket, delivery.client_socket)
        self.assertNotEqual(provider.client_mount["source"], delivery.client_mount["source"])
        self.assertTrue(provider.client_mount["read_only"])
        self.assertTrue(delivery.client_mount["read_only"])
        for projection, uid in ((provider, 65532), (delivery, 1000)):
            proxy = self.proxy(projection)
            self.assertEqual(f"{uid}:{uid}", proxy["user"])
            self.assertFalse(proxy["volumes"][0]["read_only"])
            self.assertEqual("/route", proxy["volumes"][0]["target"])
            init_role = next(iter(proxy["depends_on"]))
            initializer = projection.compose["services"][init_role]
            self.assertEqual("0:0", initializer["user"])
            self.assertEqual(["CHOWN"], initializer["cap_add"])
            self.assertEqual("none", initializer["network_mode"])
            self.assertEqual(["/route"], [mount["target"] for mount in initializer["volumes"]])
            self.assertEqual(["initialize", "--uid", str(uid), "--gid", str(uid)], initializer["command"])
            self.assertEqual({"condition": "service_healthy"}, next(iter(projection.client_depends_on.values())))

    def test_caller_prepared_bind_has_no_root_initializer_or_managed_volume(self):
        projection = self.render(self.binding("mapped"), storage=RouteDirectory(Path("/runtime/private-api")))
        proxy = self.proxy(projection)
        self.assertEqual(1, len(projection.compose["services"]))
        self.assertEqual({}, projection.compose["volumes"])
        self.assertNotIn("depends_on", proxy)
        self.assertEqual("bind", projection.client_mount["type"])
        self.assertEqual("/runtime/private-api", projection.client_mount["source"])
        self.assertFalse(projection.client_mount["bind"]["create_host_path"])
        self.assertTrue(projection.client_mount["read_only"])

    def test_full_project_identity_is_retained_while_only_long_storage_names_are_bounded(self):
        binding = self.binding("mapped")
        legacy_namespace = "p" * 63
        legacy = self.render(binding, namespace=legacy_namespace)
        self.assertTrue(legacy.client_mount["source"].startswith(legacy_namespace + "-route-"))
        long_namespace = "provider-" + "p" * 300
        first = self.render(binding, namespace=long_namespace)
        repeated = self.render(binding, namespace=long_namespace)
        distinct = self.render(binding, namespace=long_namespace + "-other-generation")
        self.assertEqual(131, len(first.client_mount["source"]))
        self.assertEqual(first.client_mount, repeated.client_mount)
        self.assertNotEqual(first.client_mount["source"], distinct.client_mount["source"])
        self.assertEqual(long_namespace, self.proxy(first)["labels"]["org.nmr-provider-tools.route"])
        self.assertEqual(legacy.client_socket, first.client_socket)

    def test_stable_route_role_labels_describe_actual_proxy_and_initializer(self):
        for storage in (NamedRouteVolume(), RouteDirectory(Path("/runtime/private-api"))):
            with self.subTest(storage=type(storage).__name__):
                projection = self.render(self.binding("mapped"), storage=storage)
                proxy = self.proxy(projection)
                self.assertEqual("proxy", proxy["labels"]["org.nmr-provider-tools.route-role"])
                self.assertEqual("provider-one", proxy["labels"]["org.nmr-provider-tools.route"])
                for service in projection.compose["services"].values():
                    self.assertEqual(proxy["labels"]["org.nmr-provider-tools.binding"],
                                     service["labels"]["org.nmr-provider-tools.binding"])
                    if service is not proxy:
                        self.assertEqual("initializer", service["labels"]["org.nmr-provider-tools.route-role"])

    def test_route_modes_preserve_authority_addresses_interface_and_frozen_resolver(self):
        for mode in ("mapped", "local", "external"):
            with self.subTest(mode=mode):
                sources = {}
                if mode == "external":
                    sources = {"/synthetic/resolver": "/frozen/api-resolv.conf",
                               "/synthetic/signing.json": "/frozen/signing.json"}
                projection = self.render(self.binding(mode), input_sources=sources)
                proxy = self.proxy(projection)
                command = proxy["command"]
                self.assertEqual("api.example:443", command[command.index("--authority") + 1])
                self.assertEqual("lo", command[command.index("--interface") + 1])
                self.assertNotIn("/frozen/signing.json", json.dumps(projection.compose))
                if mode == "local":
                    self.assertNotIn("network_mode", proxy)
                    self.assertEqual([{"external": True, "name": "b" * 64}], list(projection.compose["networks"].values()))
                else:
                    self.assertEqual("host", proxy["network_mode"])
                    self.assertEqual({}, projection.compose["networks"])
                if mode == "external":
                    self.assertNotIn("--address", command)
                    resolver = proxy["volumes"][1]
                    self.assertEqual("/frozen/api-resolv.conf", resolver["source"])
                    self.assertEqual("/etc/resolv.conf", resolver["target"])
                    self.assertTrue(resolver["read_only"])
                else:
                    self.assertEqual("api.example=127.0.0.1", command[command.index("--address") + 1])
                    self.assertEqual(1, len(proxy["volumes"]))

    def test_empty_relocation_map_preserves_original_admitted_resolver(self):
        projection = self.render(self.binding("external"))
        self.assertEqual("/synthetic/resolver", self.proxy(projection)["volumes"][1]["source"])

    def test_proxy_keeps_runtime_hardening_and_has_no_signing_or_ca_mount(self):
        projection = self.render(self.binding("mapped"))
        proxy = self.proxy(projection)
        self.assertEqual("never", proxy["pull_policy"])
        self.assertTrue(proxy["read_only"])
        self.assertTrue(proxy["init"])
        self.assertEqual(["ALL"], proxy["cap_drop"])
        self.assertEqual(["no-new-privileges:true"], proxy["security_opt"])
        self.assertEqual("private", proxy["ipc"])
        self.assertEqual("private", proxy["cgroup"])
        self.assertEqual((2, "512m", "512m", 64), (proxy["cpus"], proxy["mem_limit"], proxy["memswap_limit"], proxy["pids_limit"]))
        self.assertEqual("/opt/provider/bin/python", proxy["entrypoint"][0])
        self.assertEqual("/opt/provider/bin/python", proxy["healthcheck"]["test"][1])
        self.assertNotIn("/synthetic/signing.json", json.dumps(projection.compose))
        self.assertNotIn("ports", proxy)

    def test_invalid_identity_image_namespace_storage_and_relocation_refuse(self):
        for changes in ({"uid": 0}, {"gid": True}, {"image": "mutable:latest"},
                        {"namespace": "UPPER"}, {"interpreter": "python"}, {"storage": None},
                        {"storage": RouteDirectory(Path("/runtime/../private"))},
                        {"input_sources": {"/not-granted": "/captured"}},
                        {"input_sources": {"/synthetic/signing.json": "/captured,unsafe"}}):
            with self.subTest(changes=changes):
                with self.assertRaises(RouteProjectionRejected):
                    self.render(self.binding("mapped"), **changes)

    def test_cli_projects_selected_route_without_secret_file_access(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "binding.toml"
            source.write_bytes(definition(1))
            source.chmod(0o600)
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "provider_core.deployment_route", "render",
                 "--definition", str(source), "--binding", "fixture-0", "--image", "sha256:" + "a" * 64,
                 "--interpreter", "/opt/provider/bin/python", "--namespace", "fixture", "--uid", "65532",
                 "--gid", "65532", "--directory", "/runtime/private-api"],
                capture_output=True, text=True, check=False, timeout=5,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        projected = json.loads(result.stdout)
        self.assertEqual({"compose", "client_socket", "client_mount", "client_depends_on"}, set(projected))
        self.assertEqual("/runtime/private-api", projected["client_mount"]["source"])

    def test_initializer_preserves_exact_nonempty_private_root_without_opening_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            (root / "retained").write_bytes(b"retained")
            with patch("provider_core.deployment_route.os.geteuid", return_value=0), patch(
                "provider_core.deployment_route.os.fchown"
            ) as chown, patch("provider_core.deployment_route.os.open", wraps=os.open) as opened:
                initialize_route(os.getuid(), os.getgid(), root)
            chown.assert_not_called()
            opened.assert_not_called()
            self.assertEqual(b"retained", (root / "retained").read_bytes())

    def test_initializer_mutates_only_empty_root_owned_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            root_status = SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755)
            with patch("provider_core.deployment_route.os.geteuid", return_value=0), patch(
                "provider_core.deployment_route.os.fstat", return_value=root_status
            ), patch("provider_core.deployment_route.os.fchown") as chown:
                initialize_route(65532, 65532, root)
            self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
            self.assertEqual((65532, 65532), chown.call_args.args[1:])
            (root / "retained").write_bytes(b"retained")
            with patch("provider_core.deployment_route.os.geteuid", return_value=0), patch(
                "provider_core.deployment_route.os.fstat", return_value=root_status
            ), patch("provider_core.deployment_route.os.fchown") as chown:
                with self.assertRaises(RouteProjectionRejected):
                    initialize_route(65532, 65532, root)
            chown.assert_not_called()
            self.assertEqual(b"retained", (root / "retained").read_bytes())

    def test_initializer_rejects_symlink_without_changing_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o755)
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with patch("provider_core.deployment_route.os.geteuid", return_value=0):
                with self.assertRaises(RouteProjectionRejected):
                    initialize_route(65532, 65532, link)
            self.assertEqual(0o755, stat.S_IMODE(target.stat().st_mode))

    def test_initializer_keeps_admission_and_close_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            primary = OSError(errno.EIO, "private stat failure")
            cleanup = OSError(errno.EIO, "private close failure")
            real_close = os.close
            def close_then_fail(descriptor):
                real_close(descriptor)
                raise cleanup
            with patch("provider_core.deployment_route.os.geteuid", return_value=0), patch(
                "provider_core.deployment_route.os.fstat", side_effect=primary
            ), patch("provider_core.deployment_route.os.close", side_effect=close_then_fail):
                with self.assertRaises(BaseExceptionGroup) as failed:
                    initialize_route(65532, 65532, root)
            self.assertEqual((primary, cleanup), failed.exception.exceptions)
