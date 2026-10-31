#!/usr/bin/env python3
"""Cancellation owns container identity before any acquisition process starts."""

from tools.container_guard import require_container

require_container()

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ENGINE_STUB = r"""#!/bin/bash
set -eu
if [[ $# -lt 5 || "$1" != --config || -z "$2" || "$3" != --host || "$4" != unix:///var/run/docker.sock ]]; then
    exit 97
fi
shift 4
operation=$1
shift
printf '%s\n' "$operation $*" >> "$ENGINE_STATE/events"
case "$operation" in
  image) exit 0 ;;
  inspect) printf 'true\n'; exit ;;
  logs) exit ;;
  create|run)
    while [[ "$1" != --cidfile ]]; do shift; done
    receipt=$2
    kind=${receipt##*/}
    kind=${kind%.cid}
    if [[ "$kind" == proxy ]]; then
      identifier=$(printf 'c%.0s' {1..64})
    else
      identifier=$(printf 'd%.0s' {1..64})
    fi
    printf '%s\n' "${receipt%/*}" > "$ENGINE_STATE/scratch"
    recorded=$identifier
    returned=$identifier
    if [[ "$kind" == "${ALTER_IDENTITY:-}" ]]; then
      recorded=${RECORDED_ID-$identifier}
      returned=${RETURNED_ID-$identifier}
    fi
    if [[ "$kind" == "${CID_NUL_PHASE:-}" ]]; then
      printf '%s\0' "$recorded" > "$receipt"
    elif [[ -v CID_NO_LF_PHASE && "$kind" == "$CID_NO_LF_PHASE" ]]; then
      printf '%s' "$recorded" > "$receipt"
    else
      printf '%s\n' "$recorded" > "$receipt"
    fi
    if [[ "$kind" == proxy ]]; then
      printf '12345\n' > "${receipt%/*}/ready/port"
    fi
    printf '%s\n' "$returned"
    if [[ "$kind" == "${DELAY_IDENTITY:-}" ]]; then
      # Numeric metadata cancellation kills the unfinished client. Complete
      # identity receipts can still precede its successful return.
      : > "$ENGINE_STATE/delaying"
      while :; do sleep 60; done
    fi
    if [[ "$operation" == run && "$kind" == worker ]]; then
      while [[ ! -f "$ENGINE_STATE/removed-worker" ]]; do sleep 0.01; done
    fi
    ;;
  start)
    case "${!#}" in
      $(printf 'c%.0s' {1..64})) kind=proxy ;;
      $(printf 'd%.0s' {1..64})) kind=worker ;;
      *) exit 97 ;;
    esac
    : > "$ENGINE_STATE/started-$kind"
    if [[ "$kind" == worker ]]; then
      if [[ -n "${ALTER_STARTED_RECEIPT:-}" ]]; then
        scratch=$(<"$ENGINE_STATE/scratch")
        case "$ALTER_STARTED_RECEIPT" in
          missing) rm -- "$scratch/worker.cid" ;;
          malformed) printf 'foreign-container\n' > "$scratch/worker.cid" ;;
        esac
        printf '%s\n' "$$" > "$ENGINE_STATE/attach.pid"
        : > "$ENGINE_STATE/receipt-changed"
      fi
      if [[ -n "${WORKER_STATUS+x}" ]]; then exit "$WORKER_STATUS"; fi
      while [[ ! -f "$ENGINE_STATE/removed-worker" ]]; do sleep 0.01; done
    fi
    ;;
  rm)
    case "${!#}" in
      $(printf 'c%.0s' {1..64})) kind=proxy ;;
      $(printf 'd%.0s' {1..64})) kind=worker ;;
      *) exit 97 ;;
    esac
    if [[ -n "${REMOVE_STATUS+x}" ]]; then exit "$REMOVE_STATUS"; fi
    : > "$ENGINE_STATE/removed-$kind"
    ;;
  *) exit 3 ;;
esac
"""


class NetworkCancellationTests(unittest.TestCase):
    def test_unadmitted_started_worker_receipt_retires_owned_attach_without_container_removal(self):
        for receipt in ("missing", "malformed"):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                binary = state / "bin"
                binary.mkdir()
                (binary / "docker").symlink_to(Path(__file__).resolve())
                environment = {
                    **os.environ,
                    "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///var/run/docker.sock",
                    "DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
                    "DOCKER_CONTEXT_INPUT": "default",
                    "NETWORK_INTERFACE_INPUT": "lo",
                    "ENGINE_STATE": str(state),
                    "PATH": str(binary) + ":" + os.environ["PATH"],
                    "PYTHONPATH": str(Path.cwd()),
                    "ALTER_STARTED_RECEIPT": receipt,
                }
                process = subprocess.Popen(
                    ["bash", "tools/docker.sh", "run", "--pull", "never", "inert-image"],
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not (state / "receipt-changed").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not (state / "receipt-changed").exists() and process.poll() is not None:
                        _stdout, stderr = process.communicate(timeout=3)
                        self.fail(f"Attach fixture stopped before receipt mutation: {stderr}")
                    self.assertTrue((state / "receipt-changed").exists())
                    attach_pid = int((state / "attach.pid").read_text())
                    os.kill(attach_pid, 0)
                    process.send_signal(signal.SIGTERM)
                    _stdout, stderr = process.communicate(timeout=3)
                    self.assertEqual(143, process.returncode, msg=stderr)
                    self.assertEqual("", stderr)
                    self.assertEqual(
                        ["rm --force " + "c" * 64],
                        [event for event in (state / "events").read_text().splitlines() if event.startswith("rm ")],
                    )
                    with self.assertRaises(ProcessLookupError):
                        os.kill(attach_pid, 0)
                    scratch = Path((state / "scratch").read_text().strip())
                    self.assertTrue(scratch.exists())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                    if (state / "scratch").exists():
                        scratch = Path((state / "scratch").read_text().strip())
                        if scratch.exists():
                            shutil.rmtree(scratch)

    def test_term_during_identity_creation_removes_only_valid_receipted_containers(self):
        for phase, nul_receipt, no_lf in (
            ("proxy", False, False),
            ("worker", False, False),
            ("proxy", True, False),
            ("worker", True, False),
            ("proxy", False, True),
            ("worker", False, True),
        ):
            with self.subTest(phase=phase, nul_receipt=nul_receipt, no_lf=no_lf), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                binary = state / "bin"
                binary.mkdir()
                (binary / "docker").symlink_to(Path(__file__).resolve())
                environment = {
                    **os.environ,
                    "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///var/run/docker.sock",
                    "DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
                    "DOCKER_CONTEXT_INPUT": "default",
                    "NETWORK_INTERFACE_INPUT": "lo",
                    "ENGINE_STATE": str(state),
                    "DELAY_IDENTITY": phase,
                    "PATH": str(binary) + ":" + os.environ["PATH"],
                    "PYTHONPATH": str(Path.cwd()),
                }
                if nul_receipt:
                    environment.update(CID_NUL_PHASE=phase, ALTER_IDENTITY=phase, RETURNED_ID="")
                if no_lf:
                    environment["CID_NO_LF_PHASE"] = phase
                process = subprocess.Popen(
                    ["bash", "tools/docker.sh", "run", "--pull", "never", "inert-image"],
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not (state / "delaying").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((state / "delaying").exists())
                    scratch = Path((state / "scratch").read_text().strip())
                    identifier = {"proxy": "c" * 64, "worker": "d" * 64}[phase].encode()
                    if nul_receipt:
                        expected_cid, expected_stdout = identifier + b"\0", b"\n"
                    elif no_lf:
                        expected_cid, expected_stdout = identifier, identifier + b"\n"
                    else:
                        expected_cid = expected_stdout = identifier + b"\n"
                    self.assertEqual(expected_cid, (scratch / f"{phase}.cid").read_bytes())
                    self.assertEqual(expected_stdout, (scratch / f"{phase}-create/stdout").read_bytes())
                    self.assertFalse((state / f"started-{phase}").exists())
                    process.send_signal(signal.SIGTERM)
                    try:
                        _stdout, stderr = process.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.fail("Cancellation waited for an unidentified running worker instead of removing it")
                    self.assertEqual(143, process.returncode, msg=stderr)
                    if nul_receipt:
                        self.assertFalse((state / f"removed-{phase}").exists(), msg=stderr)
                        self.assertEqual("", stderr)
                        self.assertNotIn("ignored null byte", stderr)
                    else:
                        self.assertTrue((state / f"removed-{phase}").exists(), msg=stderr)
                    self.assertFalse((state / f"started-{phase}").exists())
                    scratch = Path((state / "scratch").read_text().strip())
                    # A cancelled create returns nonzero. Its private evidence
                    # remains even after every admitted container was removed.
                    self.assertFalse(scratch.is_symlink())
                    self.assertTrue(scratch.is_dir())
                    self.assertEqual(0o700, scratch.stat().st_mode & 0o777)
                    self.assertEqual(expected_cid, (scratch / f"{phase}.cid").read_bytes())
                    self.assertEqual(expected_stdout, (scratch / f"{phase}-create/stdout").read_bytes())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                    if (state / "scratch").exists():
                        scratch = Path((state / "scratch").read_text().strip())
                        if scratch.exists():
                            shutil.rmtree(scratch)


class ContainerIdentityTests(unittest.TestCase):
    """Only an admitted exact container identity can authorize native effects."""

    def operation(self, additions):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            binary = state / "bin"
            binary.mkdir()
            (binary / "docker").symlink_to(Path(__file__).resolve())
            environment = {
                **os.environ,
                "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///var/run/docker.sock",
                "DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
                "DOCKER_CONTEXT_INPUT": "default",
                "NETWORK_INTERFACE_INPUT": "lo",
                "ENGINE_STATE": str(state),
                "PATH": str(binary) + ":" + os.environ["PATH"],
                "PYTHONPATH": str(Path.cwd()),
                "WORKER_STATUS": "0",
                **additions,
            }
            try:
                result = subprocess.run(
                    ["bash", "tools/docker.sh", "run", "--pull", "never", "inert-image"],
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                events = (state / "events").read_text().splitlines()
                scratch = Path((state / "scratch").read_text().strip())
                return result, events, scratch.exists()
            finally:
                if (state / "scratch").exists():
                    scratch = Path((state / "scratch").read_text().strip())
                    if scratch.exists():
                        shutil.rmtree(scratch)

    def assert_identity_rejected(self, phase, returned, recorded, *, receipt_additions=None):
        # silent-defaults: allow-contract -- Omitted receipt additions use the supplied identity bytes unchanged; selected additions inject a distinct receipt fault; proof=tests/repository/test_network_cancellation.py::ContainerIdentityTests.test_nul_bearing_cid_receipts_never_authorize_start_or_removal
        result, events, retained = self.operation(
            {
                "ALTER_IDENTITY": phase,
                "RETURNED_ID": returned,
                "RECORDED_ID": recorded,
                **(receipt_additions or {}),
            }
        )
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertIn("matching its CID file", result.stderr)
        effects = [event for event in events if event.startswith(("start ", "rm "))]
        # silent-defaults: allow-contract -- Rejected proxy identity permits no effects; worker rejection permits only the already admitted proxy start and removal; proof=tests/repository/test_network_cancellation.py::ContainerIdentityTests.test_malformed_returned_and_recorded_identities_never_authorize_effects
        expected = [] if phase == "proxy" else ["start " + "c" * 64, "rm --force " + "c" * 64]
        self.assertEqual(expected, effects)
        self.assertTrue(retained, msg="Unadmitted container identity discarded its creation evidence")

    def test_malformed_returned_and_recorded_identities_never_authorize_effects(self):
        for phase in ("proxy", "worker"):
            for identity in ("foreign-container", "C" * 64, "c" * 63, "", "c" * 64 + "\n" + "d" * 64):
                with self.subTest(phase=phase, identity=identity):
                    self.assert_identity_rejected(phase, identity, identity)

    def test_mismatched_full_ids_never_authorize_start_or_removal(self):
        for phase, returned in (("proxy", "c" * 64), ("worker", "d" * 64)):
            with self.subTest(phase=phase):
                self.assert_identity_rejected(phase, returned, "e" * 64)

    def test_malformed_cid_file_cannot_authorize_returned_id_effects(self):
        for phase, returned in (("proxy", "c" * 64), ("worker", "d" * 64)):
            for recorded in ("foreign-container", returned + "\n"):
                with self.subTest(phase=phase, recorded=recorded):
                    self.assert_identity_rejected(phase, returned, recorded)

    def test_nul_bearing_cid_receipts_never_authorize_start_or_removal(self):
        for phase, identifier in (("proxy", "c" * 64), ("worker", "d" * 64)):
            with self.subTest(phase=phase):
                self.assert_identity_rejected(phase, identifier, identifier, receipt_additions={"CID_NUL_PHASE": phase})

    def test_valid_ids_preserve_worker_status_start_and_removal_order(self):
        result, events, retained = self.operation({"WORKER_STATUS": "7"})
        self.assertEqual(7, result.returncode, msg=result.stderr)
        self.assertEqual(
            [
                "start " + "c" * 64,
                "start --attach " + "d" * 64,
                "rm --force " + "d" * 64,
                "rm --force " + "c" * 64,
            ],
            [event for event in events if event.startswith(("start ", "rm "))],
        )
        self.assertFalse(retained)

    def test_failed_retirement_preserves_worker_failure_and_creation_evidence(self):
        result, events, retained = self.operation({"WORKER_STATUS": "7", "REMOVE_STATUS": "9"})
        self.assertEqual(7, result.returncode, msg=result.stderr)
        self.assertIn("worker removal", result.stderr)
        self.assertIn("proxy removal", result.stderr)
        self.assertTrue(retained)
        self.assertEqual(
            [
                "rm --force " + "d" * 64,
                "rm --force " + "c" * 64,
            ],
            [event for event in events if event.startswith("rm ")],
        )


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "docker":
        # The executable target is the read-only test source, as in
        # test_docker_client.py. Scratch remains noexec; execv preserves the
        # admitted native CLI PID while Bash runs the existing finite grammar.
        os.execv("/bin/bash", ["bash", "-euc", ENGINE_STUB, "bash", *sys.argv[1:]])
    unittest.main()
