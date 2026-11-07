"""The thin acquisition caller keeps explicit routing and literal grants."""

from tools.container_guard import require_container

require_container()

import os
import shlex
import subprocess
import unittest


class NetworkGateTests(unittest.TestCase):
    def invoke(self, interface=None):
        environment = dict(os.environ)
        environment["NMR_SELECTED_DOCKER_ENDPOINT"] = "unix:///fixture/engine.sock"
        # silent-defaults: allow-pop-default -- Strip a fixed inherited interface input so this test proves explicit selection.
        environment.pop("NETWORK_INTERFACE_INPUT", None)
        # silent-defaults: allow-pop-default -- Strip the inherited fixed origin marker without consuming its previous value.
        environment.pop("NETWORK_INTERFACE_INPUT_ORIGIN", None)
        if interface is not None:
            environment["NETWORK_INTERFACE_INPUT"] = interface
        return subprocess.run(
            ["bash", "tools/docker.sh", "--print", "run", "--image", "fixture:worker", "--",
             "--read-only", "--entrypoint", "python3", "--", "-B", "-m", "fixture",
             "--network", "literal value"],
            env=environment, text=True, capture_output=True, check=False,
        )

    def test_networked_lanes_require_an_explicit_interface(self):
        result = self.invoke()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("NETWORK_INTERFACE", result.stderr)
        self.assertEqual("", result.stdout)

    def test_networked_lanes_reject_an_unavailable_interface(self):
        result = self.invoke("missing-test0")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing-test0", result.stderr)
        self.assertEqual("", result.stdout)

    def test_preview_preserves_application_flags_and_separate_creation_grants(self):
        result = self.invoke("system")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(["acquisition_set_route", "system"], shlex.split(lines[2]))
        self.assertEqual(["--read-only", "--entrypoint", "python3"], shlex.split(lines[4][8:-1]))
        self.assertEqual(["-B", "-m", "fixture", "--network", "literal value"], shlex.split(lines[5][13:-1]))
