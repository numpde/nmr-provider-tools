"""Module entrypoints retain canonical safe refusals without supplied values."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from provider_core.deployment_binding import BindingDefinitionRejected
from provider_core.deployment_errors import BindingDefinitionRejected as CanonicalBindingRefusal
from provider_core.deployment_errors import RouteProjectionRejected as CanonicalRouteRefusal
from provider_core.deployment_route import RouteProjectionRejected
from tests.unit.normative.test_deployment_binding import definition


class DeploymentErrorCliTests(unittest.TestCase):
    def test_public_imports_share_one_error_identity(self):
        self.assertIs(BindingDefinitionRejected, CanonicalBindingRefusal)
        self.assertIs(RouteProjectionRejected, CanonicalRouteRefusal)

    def test_binding_cli_refusals_do_not_echo_untrusted_values(self):
        raw = definition(1)
        specimens = (
            b'password = "PRIVATE_INPUT_CANARY"\n' + raw,
            raw.replace(b'interface = "lo"', b'interface = "PRIVATE_INPUT_CANARY"'),
            raw.replace(b'https://api.example', b'https://user:PRIVATE_INPUT_CANARY@api.example'),
            raw.replace(b'"/synthetic/signing.json"', b'"PRIVATE_INPUT_CANARY"'),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.toml"
            for specimen in specimens:
                with self.subTest(specimen=specimen):
                    path.write_bytes(specimen)
                    path.chmod(0o600)
                    result = subprocess.run(
                        [sys.executable, "-I", "-B", "-m", "provider_core.deployment_binding",
                         "paths", "--definition", str(path), "--all"],
                        capture_output=True, text=True, check=False, timeout=10,
                    )
                    self.assertEqual(result.returncode, 2)
                    report = json.loads(result.stderr)
                    self.assertEqual(report["failure"]["exception_type"], "BindingDefinitionRejected")
                    self.assertTrue(report["failure"]["message"])
                    self.assertNotIn("PRIVATE_INPUT_CANARY", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_route_cli_uses_canonical_refusal_after_module_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.toml"
            path.write_bytes(definition(1))
            path.chmod(0o600)
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "provider_core.deployment_route", "render",
                 "--definition", str(path), "--binding", "fixture-0", "--image", "PRIVATE_INPUT_CANARY",
                 "--interpreter", "/usr/local/bin/python", "--namespace", "fixture",
                 "--uid", "65532", "--gid", "65532", "--named-volume"],
                capture_output=True, text=True, check=False, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stderr)
            self.assertEqual(report["failure"]["exception_type"], "RouteProjectionRejected")
            self.assertIn("immutable full image ID", report["failure"]["message"])
            self.assertNotIn("PRIVATE_INPUT_CANARY", result.stderr)
            self.assertEqual(result.stdout, "")
