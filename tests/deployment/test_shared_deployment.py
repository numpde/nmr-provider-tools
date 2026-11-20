"""The two shared services have independent ownership and lifecycle."""

from provider_core.container_guard import require_container

require_container()

import importlib.util
import os
import tempfile
import unittest
import pathlib
from provider_core.shared_grants import shared_grants

specification = importlib.util.spec_from_file_location("shared_deployment", "/opt/provider-tools/delivery_deployment.py")
deployment = importlib.util.module_from_spec(specification)

specification.loader.exec_module(deployment)


class SharedDeploymentTests(unittest.TestCase):
    def test_render_separates_proxy_owner_and_stable_socket_from_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = pathlib.Path(directory) / "settings.toml"
            (uid, gid) = (os.geteuid(), os.getegid())
            proxy_uid = uid + 1

            settings_path.write_text(
                f'[deployment]\n'
                f'project = "joint"\n'
                f'uid = {uid}\n'
                f'proxy_uid = {proxy_uid}\n'
                f'gid = {gid}\n'
                f'request_seconds = 10\n'
                f'[api_proxy]\n'
                f'max_connections = 32\n'
                f'log_level = "INFO"\n'
                f'allowed_authorities = ["api.example:443"]\n'
                f'[api_proxy.route]\n'
                f'mode = "mapped"\n'
                f'interface = "lo"\n'
                f'addresses = ["api.example=127.0.0.1"]\n'
                f'[outbox]\n'
                f'provider_items = 8\n'
                f'provider_bytes = 8192\n'
                f'total_items = 32\n'
                f'total_bytes = 32768\n'
                f'retry_initial_seconds = 1\n'
                f'retry_max_seconds = 30\n'
                f'log_level = "INFO"\n'
            )

            settings_path.chmod(0o600)
            plan = deployment.render_shared(settings_path, settings_path, "sha256:" + "a" * 64)
            grants = shared_grants(settings_path)
            (proxy, outbox) = (plan["proxy"]["services"]["proxy"], plan["outbox"]["services"]["delivery"])

            self.assertEqual(proxy["user"], f"{proxy_uid}:{gid}")
            self.assertEqual(outbox["user"], f"{uid}:{gid}")

            self.assertEqual(
                {"external": True, "name": "joint-proxy-socket"},
                plan["outbox"]["volumes"]["joint-proxy-socket"]
            )

            self.assertEqual({"name": "joint-proxy-socket"}, plan["proxy"]["volumes"]["joint-proxy-socket"])
            self.assertTrue(next(volume for volume in proxy["volumes"] if volume["target"] == "/settings")["read_only"])
            self.assertEqual(next(volume for volume in outbox["volumes"] if volume["target"] == "/settings")["source"], directory)
            self.assertEqual(proxy["command"][proxy["command"].index("--socket-group-id") + 1], str(gid))
            self.assertNotIn("proxy", outbox["depends_on"])
            self.assertEqual("joint-proxy-socket", grants.proxy_volume)
            self.assertEqual("joint-outbox-socket", grants.outbox_volume)
            self.assertEqual("/run/nmr-api-proxy/proxy.sock", str(grants.proxy_socket))
            self.assertEqual("/run/nmr-api-outbox/http.sock", str(grants.endpoint("https://api.example", "provider:test").socket_path))
            self.assertEqual([str(gid)], grants.compose()["group_add"])
            self.assertTrue(all((mount["read_only"] and mount["volume"]["nocopy"]) for mount in grants.compose()["mounts"]))
            settings_path.write_text(settings_path.read_text().replace(f"proxy_uid = {proxy_uid}", f"proxy_uid = {uid}"))

            with self.assertRaises(deployment.DeliveryDeploymentRejected):
                deployment.render_shared(settings_path, settings_path, "sha256:" + "a" * 64)
