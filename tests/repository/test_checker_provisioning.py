"""Check the maintained preparation caller, source identity, and image contents."""

from tools.container_guard import require_container

require_container()

import importlib.metadata
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.repository.quality.sources import ROOT


class CheckerProvisioningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("tools", "requirements", "containers"):
            shutil.copytree(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "host", self.root / "host")
        shutil.copyfile(ROOT / "Makefile", self.root / "Makefile")
        self.environment = os.environ | {"PATH": f"{ROOT / 'tests/repository/fixtures/docker-context'}:{os.environ['PATH']}"}

    def shell(self, source):
        return subprocess.run(
            ["bash", "-eu", "-o", "pipefail", "-c", source, "bash", str(self.root)],
            env=self.environment, capture_output=True, text=True, check=False,
        )

    def invoke_make(self, target, *, wheelhouse, interface):
        # This test selects inputs; the native lane qualifies bootstrap builds.
        arguments = ["make", "--no-print-directory", "-o", "checks/bootstrap/build", target, "DOCKER_CONTEXT=default"]
        if wheelhouse is not None:
            arguments.append(f"WHEELHOUSE={wheelhouse}")
        if interface is not None:
            arguments.append(f"NETWORK_INTERFACE={interface}")
        return subprocess.run(arguments, cwd=self.root, env=self.environment, capture_output=True, text=True, check=False)

    def test_explicit_empty_and_environment_only_selectors_do_not_authorize_acquisition(self):
        self.environment |= {"WHEELHOUSE": str(self.root), "WHEELHOUSE_INPUT": str(self.root),
                             "WHEELHOUSE_INPUT_ORIGIN": "command line", "NETWORK_INTERFACE_INPUT": "lo"}
        cases = (("", "lo", "WHEELHOUSE cannot be empty"),
                 (None, "lo", "WHEELHOUSE must be set on the make command line"),
                 (str(self.root), "", "is not an available network interface"),
                 (str(self.root), "missing-interface", "is not an available network interface"))
        for wheelhouse, interface, diagnostic in cases:
            with self.subTest(wheelhouse=wheelhouse, interface=interface):
                result = self.invoke_make("checks/dependencies/download", wheelhouse=wheelhouse, interface=interface)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(diagnostic, result.stderr)

    def test_fingerprint_binds_source_bytes_and_modes_and_refuses_missing_inputs(self):
        source = 'root=$1; source "$root/host/tools/checker_inputs.sh"; checker_fingerprint checker'
        first = self.shell(source)
        self.assertEqual(0, first.returncode, msg=first.stderr)
        target = self.root / "containers/checker/Dockerfile"
        target.write_text(target.read_text() + "# changed input\n")
        second = self.shell(source)
        self.assertNotEqual(first.stdout, second.stdout)
        target.chmod(0o600)
        self.assertNotEqual(second.stdout, self.shell(source).stdout)
        target.unlink()
        self.assertNotEqual(0, self.shell(source).returncode)

    def test_failed_fingerprint_producer_cannot_publish_partial_digest(self):
        result = self.shell('''root=$1
source "$root/host/tools/checker_inputs.sh"
sha256sum() {
    if [[ $# != 0 && $2 == host/tools/base_image.sh ]]; then return 7; fi
    command sha256sum "$@"
}
if fingerprint=$(checker_fingerprint checker); then exit 99; else exit "$?"; fi
''')
        self.assertEqual(7, result.returncode, msg=result.stderr)

    def test_source_mutation_during_capture_reports_the_changed_inputs(self):
        result = self.shell('''root=$1
export DOCKER_CONTEXT_INPUT=default DOCKER_CONTEXT_INPUT_ORIGIN='command line'
cp() {
    if [[ $* == *host/tools/base_image.sh* ]]; then printf '\\n# changed during copy\\n' >> "$root/host/tools/base_image.sh"; fi
    command cp "$@"
}
export -f cp
bash "$root/host/tools/checker.sh" bootstrap-build
''')
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertIn("Cannot build checker: source inputs changed during capture", result.stderr)

    def test_image_admission_requires_matching_exact_metadata_and_preserves_cancellation(self):
        for scenario, expected in (("valid", 0), ("wrong-label", 2), ("nul", 2), ("cancelled", 143), ("missing", 1)):
            with self.subTest(scenario=scenario):
                result = self.shell('''root=$1
source "$root/host/tools/checker_inputs.sh"
operation_directory=$root
operation_diagnostic=
identifier=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
fingerprint=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
checker_metadata() {
    local label=$fingerprint
    if [[ SCENARIO == wrong-label ]]; then label=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; fi
    if [[ SCENARIO == nul ]]; then printf '%s %s\\0\\n' "$identifier" "$label" > "$1"
    else printf '%s %s\\n' "$identifier" "$label" > "$1"; fi
    if [[ SCENARIO == cancelled ]]; then return 143; fi
    if [[ SCENARIO == missing ]]; then return 1; fi
}
if checker_admit_image "$fingerprint" checker; then [[ $checker_image == "$identifier" ]]; else exit "$?"; fi
'''.replace("SCENARIO", scenario))
                self.assertEqual(expected, result.returncode, msg=result.stderr)

    def test_owned_checker_provides_make_without_ruff(self):
        self.assertIsNotNone(shutil.which("make"))
        self.assertIsNone(shutil.which("ruff"))
        with self.assertRaises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.distribution("ruff")

    def test_owned_checker_uses_the_sdk_installer_release(self):
        record = json.loads((ROOT / "requirements/installer-release.json").read_text())
        self.assertEqual(record["name"].split("-")[1], importlib.metadata.version("pip"))

    def test_sdk_acquisition_prepares_the_stdlib_bootstrap_before_installer_workers(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "--dry-run", "packages/provider-core/download",
             "DOCKER_CONTEXT=default", f"WHEELHOUSE={self.root}", "NETWORK_INTERFACE=lo"],
            cwd=self.root, env=self.environment, check=True, capture_output=True, text=True,
        )
        self.assertLess(result.stdout.index("bash host/tools/checker.sh prepare"),
                        result.stdout.index("bash host/tools/provider_core.sh download"))
