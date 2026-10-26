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
        }

    def invocation(self, scenario):
        script = '''set -euo pipefail
root=$1
source "$root/tools/checker_operation.sh"
checker_begin
printf '%s\\n' "$operation_directory" "$docker_client_directory" > "$PROBE_REPORT"
engine=(bash "$PROBE_ENGINE")
if [[ $PROBE_SCENARIO == unknown-retirement ]]; then
    inference_retirement_confirmed=false
    checker_call 'forbidden dispatch' 1 stdio true
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

    def test_success_removes_owned_containers_before_temporary_inputs(self):
        result = self.run_probe("success")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(["create", "start", "ps", "rm", "ps"], (self.directory / "calls").read_text().splitlines())
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
