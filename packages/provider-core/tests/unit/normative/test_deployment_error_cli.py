"""Module entrypoints retain canonical safe refusals without supplied values."""

import json
import subprocess
import sys
import tempfile
import unittest
import os
import pathlib

from provider_core.deployment_binding import BindingDefinitionRejected
from provider_core.deployment_errors import BindingDefinitionRejected as CanonicalBindingRefusal
from provider_core.deployment_errors import RouteProjectionRejected as CanonicalRouteRefusal
from provider_core.deployment_route import RouteProjectionRejected
from tests.unit.normative.test_deployment_binding import definition


class DeploymentErrorCliTests(unittest.TestCase):
    def test_public_imports_share_one_error_identity(self):
        self.assertIs(BindingDefinitionRejected, CanonicalBindingRefusal)
        self.assertIs(RouteProjectionRejected, CanonicalRouteRefusal)

    def _binding_cli_refusals(self):
        raw = definition(1)

        specimens = (
            b'password = "PRIVATE_INPUT_CANARY"\n' + raw,
            raw.replace(b'interface = "lo"', b'interface = "PRIVATE_INPUT_CANARY"'),
            raw.replace(b'https://api.example', b'https://user:PRIVATE_INPUT_CANARY@api.example'),
            raw.replace(b'"/synthetic/signing.json"', b'"PRIVATE_INPUT_CANARY"'),
        )

        with tempfile.TemporaryDirectory() as directory:
            binding_path = pathlib.Path(directory) / "binding.toml"

            for specimen in specimens:
                with self.subTest(specimen=specimen):
                    binding_path.write_bytes(specimen)
                    binding_path.chmod(0o600)

                    completed_process = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            "-m",
                            "provider_core.deployment_binding",
                            "paths",
                            "--definition",
                            str(binding_path),
                            "--all"
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )

                    yield completed_process

    def test_binding_cli_reports_canonical_refusals(self):
        for completed_process in self._binding_cli_refusals():
            self.assertEqual(completed_process.returncode, 2)
            report = json.loads(completed_process.stderr)

            self.assertEqual(report["failure"]["exception_type"], "BindingDefinitionRejected")
            self.assertTrue(report["failure"]["message"])
            self.assertEqual(completed_process.stdout, "")

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "PRIVACY_TESTS=0")
    def test_binding_cli_refusals_do_not_echo_untrusted_values(self):
        for completed_process in self._binding_cli_refusals():
            self.assertNotIn("PRIVATE_INPUT_CANARY", completed_process.stderr)

    def _route_cli_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            binding_path = pathlib.Path(directory) / "binding.toml"

            binding_path.write_bytes(definition(1))
            binding_path.chmod(0o600)

            completed_process = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "provider_core.deployment_route",
                    "render",
                    "--definition",
                    str(binding_path),
                    "--binding",
                    "fixture-0",
                    "--image",
                    "PRIVATE_INPUT_CANARY",
                    "--interpreter",
                    "/usr/local/bin/python",
                    "--namespace",
                    "fixture",
                    "--uid",
                    "65532",
                    "--gid",
                    "65532",
                    "--named-volume"
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            return completed_process

    def test_route_cli_uses_canonical_refusal_after_module_execution(self):
        completed_process = self._route_cli_refusal()

        self.assertEqual(completed_process.returncode, 2)
        report = json.loads(completed_process.stderr)

        self.assertEqual(report["failure"]["exception_type"], "RouteProjectionRejected")
        self.assertIn("immutable full image ID", report["failure"]["message"])
        self.assertEqual(completed_process.stdout, "")

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "PRIVACY_TESTS=0")
    def test_route_cli_refusals_do_not_echo_untrusted_values(self):
        completed_process = self._route_cli_refusal()

        self.assertNotIn("PRIVATE_INPUT_CANARY", completed_process.stderr)
