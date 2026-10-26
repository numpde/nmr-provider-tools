"""Check the maintained preparation caller, source identity, and image contents."""

from tools.container_guard import require_container

require_container()

import importlib.metadata
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
        shutil.copyfile(ROOT / "Makefile", self.root / "Makefile")
        self.record = self.root / "arguments"
        self.environment = os.environ | {"PROVISION_ARGUMENTS": str(self.record)}

    def shell(self, source):
        return subprocess.run(
            ["bash", "-eu", "-o", "pipefail", "-c", source, "bash", str(self.root)],
            capture_output=True, text=True, check=False,
        )

    def invoke_make(self, target, *, wheelhouse, interface):
        arguments = ["make", "--no-print-directory", target]
        if wheelhouse is not None:
            arguments.append(f"WHEELHOUSE={wheelhouse}")
        if interface is not None:
            arguments.append(f"NETWORK_INTERFACE={interface}")
        return subprocess.run(arguments, cwd=self.root, env=self.environment, capture_output=True, text=True, check=False)

    def test_network_caller_supplies_worker_grants_and_literal_shared_cache(self):
        (self.root / "tools/docker.sh").write_text('printf "%s\\0" "$@" > "$PROVISION_ARGUMENTS"\n')
        cache = self.root / "cache $literal 'quoted'"
        result = self.invoke_make("checks/dependencies/download", wheelhouse=str(cache), interface="lo")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        arguments = self.record.read_bytes().decode().split("\0")[:-1]
        self.assertEqual("run", arguments[0])
        self.assertIn(f"type=bind,src={cache},dst=/wheelhouse", arguments)
        self.assertIn("NMR_PROVIDER_TOOLS_IN_CONTAINER=1", arguments)
        for flag, value in (("--pull", "never"), ("--user", f"{os.getuid()}:{os.getgid()}"), ("--cap-drop", "ALL"),
                            ("--security-opt", "no-new-privileges:true"), ("--memory", "512m"),
                            ("--memory-swap", "512m"), ("--cpus", "2"), ("--pids-limit", "64")):
            self.assertEqual(value, arguments[arguments.index(flag) + 1])
        self.assertIn("--read-only", arguments)
        self.assertIn("download", arguments)
        self.assertFalse(any("docker.sock" in value for value in arguments))

    def test_offline_check_ignores_network_selector_and_grants_cache_read_only(self):
        (self.root / "tools/checker_operation.sh").write_text('''checker_begin() { operation_directory=$root/probe; }
checker_run() { printf '%s\\0' "$@" > "$PROVISION_ARGUMENTS"; }
''')
        result = self.invoke_make("checks/dependencies/check", wheelhouse=".", interface="missing-interface")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        arguments = self.record.read_bytes().decode().split("\0")[:-1]
        self.assertEqual(["60", "--network", "none"], arguments[:3])
        self.assertIn(f"type=bind,src={self.root},dst=/wheelhouse,readonly", arguments)

    def test_explicit_empty_and_environment_only_selectors_do_not_authorize_acquisition(self):
        self.environment |= {"WHEELHOUSE": str(self.root), "WHEELHOUSE_INPUT": str(self.root),
                             "WHEELHOUSE_INPUT_ORIGIN": "command line", "NETWORK_INTERFACE_INPUT": "lo"}
        for wheelhouse, interface in (("", "lo"), (None, "lo"), (str(self.root), ""), (str(self.root), "missing-interface")):
            with self.subTest(wheelhouse=wheelhouse, interface=interface):
                result = self.invoke_make("checks/dependencies/download", wheelhouse=wheelhouse, interface=interface)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(self.record.exists())

    def test_fingerprint_binds_source_bytes_and_modes_and_refuses_missing_inputs(self):
        source = 'root=$1; source "$root/tools/checker_inputs.sh"; checker_fingerprint'
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
source "$root/tools/checker_inputs.sh"
sha256sum() {
    if [[ $# != 0 && $2 == tools/base_image.sh ]]; then return 7; fi
    command sha256sum "$@"
}
if fingerprint=$(checker_fingerprint); then exit 99; else exit "$?"; fi
''')
        self.assertEqual(7, result.returncode, msg=result.stderr)

    def test_source_mutation_during_capture_refuses_build_dispatch(self):
        (self.root / "tools/checker_operation.sh").write_text('''checker_begin() {
    operation_directory=$root/probe
    operation_diagnostic=
    mkdir "$operation_directory"
}
checker_run() { touch "$root/dispatched"; }
''')
        result = self.shell('''root=$1
export WHEELHOUSE_INPUT=$root WHEELHOUSE_INPUT_ORIGIN='command line'
cp() {
    if [[ $* == *tools/base_image.sh* ]]; then printf '\\n# changed during copy\\n' >> tools/base_image.sh; fi
    command cp "$@"
}
export -f cp
bash "$root/tools/checker.sh" build
''')
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertFalse((self.root / "dispatched").exists())

    def test_image_admission_requires_matching_exact_metadata_and_preserves_cancellation(self):
        for scenario, expected in (("valid", 0), ("wrong-label", 2), ("nul", 2), ("cancelled", 143), ("missing", 1)):
            with self.subTest(scenario=scenario):
                result = self.shell('''root=$1
source "$root/tools/checker_inputs.sh"
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
if checker_admit_image "$fingerprint"; then [[ $checker_image == "$identifier" ]]; else exit "$?"; fi
'''.replace("SCENARIO", scenario))
                self.assertEqual(expected, result.returncode, msg=result.stderr)

    def test_owned_checker_provides_make_without_ruff(self):
        self.assertIsNotNone(shutil.which("make"))
        self.assertIsNone(shutil.which("ruff"))
        with self.assertRaises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.distribution("ruff")
