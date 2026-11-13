"""Fault the production cleanup owner with an offline engine protocol fixture."""

from tools.container_guard import require_container

require_container()

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.repository.quality.sources import ROOT


class CheckerOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.engine = self.directory / "engine.sh"
        self.engine.write_text('''set -eu
printf '%s\\n' "$1" >> "$PROBE_CALLS"
identifier=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
case "$1" in
    create)
        shift
        while [[ $1 != --cidfile ]]; do shift; done
        printf '%s' "$identifier" > "$2"
        touch "$PROBE_STATE"
        printf '%s\\n' "$identifier"
        if [[ $PROBE_SCENARIO == uncertain-create ]]; then exit 7; fi
        ;;
    start)
        if [[ $PROBE_SCENARIO == primary-failure ]]; then exit 23; fi
        if [[ $PROBE_SCENARIO == attachment-timeout ]]; then sleep 5; fi
        ;;
    ps)
        if [[ $PROBE_SCENARIO == enumeration-failure ]]; then exit 19; fi
        if [[ $PROBE_SCENARIO == malformed-list ]]; then printf '%s\\0\\n' "$identifier"; exit; fi
        if [[ -e $PROBE_STATE ]]; then printf '%s\\n' "$identifier"; fi
        ;;
    rm)
        touch "$PROBE_CLEANUP_READY"
        if [[ $PROBE_SCENARIO == repeated-signals ]]; then sleep 1; fi
        case "$PROBE_SCENARIO" in
            residue|primary-failure) exit 0 ;;
            *) rm -- "$PROBE_STATE" ;;
        esac
        ;;
    *) exit 99 ;;
esac
''')
        self.environment = os.environ | {
            "PROBE_CALLS": str(self.directory / "calls"),
            "PROBE_STATE": str(self.directory / "state"),
            "PROBE_REPORT": str(self.directory / "report"),
            "PROBE_CLEANUP_READY": str(self.directory / "ready"),
            "PROBE_ENGINE": str(self.engine),
            "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///var/run/docker.sock",
            "DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
            "DOCKER_CONTEXT_INPUT": "default",
        }

    def invocation(self, scenario):
        script = '''set -euo pipefail
root=$1
source "$root/host/tools/checker_operation.sh"
checker_begin
printf '%s\\n' "$operation_directory" "$docker_client_directory" > "$PROBE_REPORT"
engine=(bash "$PROBE_ENGINE")
if [[ $PROBE_SCENARIO == unknown-retirement ]]; then
    inference_retirement_confirmed=false
    checker_call 'forbidden dispatch' 1 stdio true
fi
if [[ $PROBE_SCENARIO == settled-stream-failure ]]; then
    printf 'captured build input\\n' > "$operation_directory/source"
    checker_call 'settled failed stream build stage' 2 stdio bash -o pipefail -c '
        cat "$1" | { cat > "$2"; exit 23; }
    ' bash "$operation_directory/source" "$PROBE_REPORT.stream"
fi
budget=10
if [[ $PROBE_SCENARIO == attachment-timeout ]]; then budget=1; fi
if checker_run "$budget" inert-image; then exit 0; else exit "$?"; fi
'''
        return ["bash", "-c", script, "bash", str(ROOT)], self.environment | {"PROBE_SCENARIO": scenario}

    def owned_paths(self):
        paths = [Path(line) for line in (self.directory / "report").read_text().splitlines()]
        for path in paths:
            self.assertEqual(Path("/tmp"), path.parent)
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return paths

    def run_probe(self, scenario):
        command, environment = self.invocation(scenario)
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=15, check=False)

    def identity_invocation(self, scenario):
        shadow = self.directory / "identity-checkout"
        (shadow / "host/tools").mkdir(parents=True)
        for name in ("checker_operation.sh", "docker_client.sh", "inference_native.sh", "image_lifecycle.sh"):
            shutil.copyfile(ROOT / "host/tools" / name, shadow / "host/tools" / name)
        acquisition = shadow / "bootstrap/acquisition/acquisition_tool"
        acquisition.mkdir(parents=True)
        # The published owner is an external port here. Its own tests qualify
        # probing; this fixture exposes its call, result, and retirement receipt.
        (acquisition / "owner.sh").write_text('''printf 'source\\n' >> "$PROBE_IDENTITY_EVENTS"
acquisition_project_ids() {
    printf 'project\\n' >> "$PROBE_IDENTITY_EVENTS"
    printf '%s\\n' "${@:2:3}" "${@:6}" > "$PROBE_IDENTITY_ARGUMENTS"
    case "$PROBE_SCENARIO" in
        missing-receipt) return 0 ;;
        malformed-receipt) printf 'retired\\nextra\\n' > "$5"; return 0 ;;
    esac
    printf 'retired\\n' > "$5"
    case "$PROBE_SCENARIO" in
        identity-failure) return 23 ;;
        identity-signal) return 143 ;;
    esac
    local -n fixture_output=$1
    fixture_output=(17 29)
}
''')
        script = '''set -euo pipefail
root=$1
python3() {
    printf 'verify\\n' >> "$PROBE_IDENTITY_EVENTS"
    if [[ $PROBE_SCENARIO == verification-failure ]]; then return 31; fi
}
source "$root/host/tools/checker_operation.sh"
checker_begin
printf '%s\\n' "$operation_directory" "$docker_client_directory" > "$PROBE_REPORT"
engine=(bash "$PROBE_ENGINE")
checker_host_uid=old-uid
checker_host_gid=old-gid
if checker_host_identity pinned-image /probe/python 1001 1002; then status=0; else status=$?; fi
printf '%s\\n' "$status" "$checker_host_uid" "$checker_host_gid" "$inference_retirement_confirmed" > "$PROBE_IDENTITY_RESULT"
if [[ $PROBE_SCENARIO == identity-success ]]; then
    checker_call 'settled identity follow-up' 1 stdio bash -c 'printf "native\\n" >> "$PROBE_IDENTITY_EVENTS"'
fi
if [[ $PROBE_SCENARIO == missing-receipt || $PROBE_SCENARIO == malformed-receipt ]]; then
    if checker_call 'forbidden native dispatch' 1 stdio bash -c 'printf "native\\n" >> "$PROBE_IDENTITY_EVENTS"'; then next_native=0; else next_native=$?; fi
    if checker_host_identity next-image /probe/python 1001 1002; then next_identity=0; else next_identity=$?; fi
    printf '%s\\n' "$next_native" "$next_identity" "$inference_retirement_confirmed" >> "$PROBE_IDENTITY_RESULT"
fi
exit "$status"
'''
        environment = self.environment | {
            "PROBE_SCENARIO": scenario,
            "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///fixture-selected.sock",
            "PROBE_IDENTITY_EVENTS": str(self.directory / "identity-events"),
            "PROBE_IDENTITY_ARGUMENTS": str(self.directory / "identity-arguments"),
            "PROBE_IDENTITY_RESULT": str(self.directory / "identity-result"),
        }
        return ["bash", "-c", script, "bash", str(shadow)], environment

    def run_identity_probe(self, scenario):
        command, environment = self.identity_invocation(scenario)
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=15, check=False)

    def test_shared_identity_owner_receives_selected_endpoint_policy_and_map_order(self):
        result = self.run_identity_probe("identity-success")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(["verify", "source", "project", "native"], (self.directory / "identity-events").read_text().splitlines())
        self.assertEqual(
            ["2", "unix:///fixture-selected.sock", "pinned-image", "--interpreter", "/probe/python",
             "--user-uid", "0", "--user-gid", "0", "--memory-bytes", "67108864", "--pids-limit", "16",
             "--nano-cpus", "1000000000", "--map", "uid", "inverse", "1001", "--map", "gid", "inverse", "1002"],
            (self.directory / "identity-arguments").read_text().splitlines(),
        )
        self.assertEqual(["0", "17", "29", "true"], (self.directory / "identity-result").read_text().splitlines())
        self.assertTrue(all(not path.exists() for path in self.owned_paths()))

    def assert_identity_receipt_blocks_dispatch(self, scenario):
        result = self.run_identity_probe(scenario)
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertEqual(["verify", "source", "project"], (self.directory / "identity-events").read_text().splitlines())
        self.assertEqual(
            ["2", "old-uid", "old-gid", "true", "2", "2", "true"],
            (self.directory / "identity-result").read_text().splitlines(),
        )
        self.assertTrue(all(path.is_dir() for path in self.owned_paths()))
        self.assertIn("acquisition retirement is unconfirmed", result.stderr)

    def test_missing_identity_receipt_blocks_dispatch_and_retains_inputs(self):
        self.assert_identity_receipt_blocks_dispatch("missing-receipt")

    def test_malformed_identity_receipt_blocks_dispatch_and_retains_inputs(self):
        self.assert_identity_receipt_blocks_dispatch("malformed-receipt")

    def assert_settled_identity_failure(self, scenario, status):
        result = self.run_identity_probe(scenario)
        self.assertEqual(status, result.returncode, msg=result.stderr)
        self.assertEqual(
            [str(status), "old-uid", "old-gid", "true"],
            (self.directory / "identity-result").read_text().splitlines(),
        )
        self.assertTrue(all(not path.exists() for path in self.owned_paths()))

    def test_settled_identity_failure_preserves_status_and_prior_ids(self):
        self.assert_settled_identity_failure("identity-failure", 23)

    def test_settled_identity_signal_preserves_status_and_prior_ids(self):
        self.assert_settled_identity_failure("identity-signal", 143)

    def test_identity_verification_failure_stops_before_sourcing_or_probe(self):
        result = self.run_identity_probe("verification-failure")
        self.assertEqual(31, result.returncode, msg=result.stderr)
        self.assertEqual(["verify"], (self.directory / "identity-events").read_text().splitlines())
        self.assertEqual(["31", "old-uid", "old-gid", "true"], (self.directory / "identity-result").read_text().splitlines())
        self.assertTrue(all(not path.exists() for path in self.owned_paths()))

    def test_success_removes_owned_containers_before_temporary_inputs(self):
        result = self.run_probe("success")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(["create", "start", "ps", "rm", "ps"], (self.directory / "calls").read_text().splitlines())
        self.assertTrue(all(not path.exists() for path in self.owned_paths()))

    def test_successful_build_retires_images_only_after_cleanup_and_lock_release(self):
        shadow = self.directory / "checkout"
        (shadow / "host/tools").mkdir(parents=True)
        for name in ("checker_operation.sh", "docker_client.sh", "inference_native.sh", "image_lifecycle.sh"):
            shutil.copyfile(ROOT / "host/tools" / name, shadow / "host/tools" / name)
        (shadow / "host/tools/image_retirement.sh").write_text('''set -eu
[[ $1 == --automatic ]]
[[ ! -e $PROBE_STATE ]]
readarray -t paths < "$PROBE_REPORT"
[[ ! -e ${paths[0]} && ! -e ${paths[1]} ]]
root=$(cd -- "$(dirname -- "$0")/../.." && pwd -P)
exec {borrow}< "$root"
flock --exclusive --nonblock "$borrow"
printf 'retired\\n' > "$PROBE_REPORT.retired"
''')
        command, environment = self.invocation("success")
        command[-1] = str(shadow)
        command[2] = command[2].replace('budget=10', 'image_retire_after_success=true\nbudget=10')
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("retired\n", Path(self.environment["PROBE_REPORT"] + ".retired").read_text())
        self.assertTrue(all(not path.exists() for path in self.owned_paths()))

    def test_false_success_removal_and_failed_enumeration_retain_inputs(self):
        for scenario in ("residue", "enumeration-failure", "malformed-list"):
            with self.subTest(scenario=scenario):
                result = self.run_probe(scenario)
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertTrue(all(path.is_dir() for path in self.owned_paths()))
                self.assertIn("Retained operation", result.stderr)

    def test_primary_failure_survives_cleanup_failure(self):
        result = self.run_probe("primary-failure")
        self.assertEqual(23, result.returncode, msg=result.stderr)
        self.assertTrue(all(path.is_dir() for path in self.owned_paths()))

    def test_failed_create_is_never_started_and_retains_even_after_empty_listing(self):
        result = self.run_probe("uncertain-create")
        self.assertEqual(7, result.returncode, msg=result.stderr)
        self.assertNotIn("start", (self.directory / "calls").read_text().splitlines())
        self.assertTrue(all(path.is_dir() for path in self.owned_paths()))

    def test_unknown_local_retirement_forbids_next_dispatch_and_cleanup(self):
        result = self.run_probe("unknown-retirement")
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertFalse((self.directory / "calls").exists())
        self.assertTrue(all(path.is_dir() for path in self.owned_paths()))

    def test_settled_failed_stream_releases_temporary_inputs(self):
        result = self.run_probe("settled-stream-failure")
        self.assertEqual(23, result.returncode, msg=result.stderr)
        self.assertEqual("captured build input\n", Path(self.environment["PROBE_REPORT"] + ".stream").read_text())
        self.assertEqual(["ps", "ps"], (self.directory / "calls").read_text().splitlines())
        self.assertTrue(all(not path.exists() for path in self.owned_paths()))

    def test_attachment_deadline_retires_client_and_container_before_releasing_inputs(self):
        result = self.run_probe("attachment-timeout")
        self.assertEqual(124, result.returncode, msg=result.stderr)
        self.assertIn("rm", (self.directory / "calls").read_text().splitlines())
        self.assertTrue(all(not path.exists() for path in self.owned_paths()))

    def test_repeated_signals_do_not_interrupt_teardown(self):
        command, environment = self.invocation("repeated-signals")
        with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            deadline = time.monotonic() + 10
            while not (self.directory / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.directory / "ready").exists())
            process.send_signal(signal.SIGTERM)
            process.send_signal(signal.SIGHUP)
            _, diagnostic = process.communicate(timeout=10)
        self.assertEqual(0, process.returncode, msg=diagnostic)
        self.assertTrue(all(not path.exists() for path in self.owned_paths()))
