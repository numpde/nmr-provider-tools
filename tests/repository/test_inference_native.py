#!/usr/bin/env python3
"""Probe captured client ownership using finite offline process and file faults."""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch


class CapturedNativeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = self.root / "inference_native.sh"
        self.helper.write_bytes(Path("tools/inference_native.sh").read_bytes())
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("mv", "head", "rm"):
            (self.bin / name).symlink_to(Path(__file__).resolve())
        self.observation = self.root / "client.json"
        self.control = self.root / "control.json"
        self.environment = os.environ | {
            "PATH": str(self.bin) + ":" + os.environ["PATH"],
            "PYTHONPATH": str(Path.cwd()),
            "NATIVE_OBSERVATION": str(self.observation),
            "NATIVE_CONTROL": str(self.control),
            # Each probe owns its intended result; lost fixture wiring must
            # fail in the client rather than silently manufacture success.
            "NATIVE_STATUS": "0",
            # Behavior is an owned scenario input; ambient values cannot select
            # a stream or lifetime fault for an ordinary client.
            "NATIVE_BEHAVIOR": "",
            # Ordinary publication is owned here; only mv consumes this
            # selector, while rm/head keep their separate fault controls.
            "NATIVE_PUBLICATION_FAILURE": "",
            # Status receipt mutation is owned by the fixture and consumed
            # only when mv publishes status or head reads that status file.
            "NATIVE_STATUS_MUTATION": "",
        }

    def test_native_status_is_owned_at_construction_and_required_by_the_client(self):
        with patch.dict(os.environ, {"NATIVE_STATUS": "37"}):
            probe = CapturedNativeTests()
            probe.setUp()
            try:
                self.assertEqual("0", probe.environment["NATIVE_STATUS"])
                result = probe.run_native()
                self.assertEqual(0, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual("", result.stderr)
                probe.assert_client_retired()
            finally:
                self.assertTrue(probe.doCleanups())

        environment = self.environment.copy()
        del environment["NATIVE_STATUS"]
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "native-client"],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("NATIVE_STATUS", result.stderr)
        self.assertEqual("", result.stdout)

    def test_control_failures_return_before_draining_full_public_stderr(self):
        # The harness consumes the returned fields privately. It proves that
        # callers can finish fact-permitted cleanup before publishing text.
        cases = (
            ("mktemp", "mktemp", 28, "true", "private evidence directory creation failed", "0", "discard"),
            ("mkfifo", "mkfifo", 29, "true", "private output setup failed", "0", "discard"),
            ("ready", "mv", 1, "false", "exact live identity", "0", "discard"),
            ("release", "mv", 29, "true", "parent release could not be published", "0", "discard"),
            ("status", "mv", 1, "true", "control publication failed", "0", "discard"),
            ("remove-success", "rm", 23, "true", "evidence directory removal", "0", "discard"),
            ("remove-failure", "rm", 7, "true", "control files", "7", "discard"),
            ("identity", "grep", 2, "true", "exactly one full immutable image ID", "0", "image-id"),
        )
        noise = 'printf "%s\\n" "$NATIVE_IO_PHASE" > "$NATIVE_IO_ENTERED"; head -c 65536 /dev/zero >&2; '
        for phase, utility, expected_status, expected_fact, reason, native_status, result in cases:
            with self.subTest(phase=phase):
                entered = self.root / "io-entered"
                receipt = self.root / "io-return"
                diagnostic = self.root / "io-diagnostic"
                sentinel = self.root / "caller-sentinel"
                failed_anchor = self.root / "failed-anchor"
                for path in (entered, receipt, diagnostic, failed_anchor, self.observation):
                    path.unlink(missing_ok=True)
                sentinel.write_text("owned caller input\n")
                if utility == "mv":
                    body = (
                        '() { if [[ "${@: -1}" == */status."$NATIVE_IO_PHASE" '
                        '|| ( "$NATIVE_IO_PHASE" == status && "${@: -1}" == */status ) ]]; then '
                        'if [[ "$NATIVE_IO_PHASE" == ready ]]; then printf "%s\\n" "$BASHPID" > "$NATIVE_IO_ANCHOR"; fi; '
                        + noise
                        + 'return 29; fi; command mv "$@"; }'
                    )
                elif utility == "rm":
                    body = "() { " + noise + "return 23; }"
                else:
                    code = {"mktemp": 28, "mkfifo": 29, "grep": 31}[utility]
                    body = "() { " + noise + f"return {code}; }}"
                script = (
                    'source "$1"; inference_diagnostic=; '
                    "trap 'status=$?; trap - EXIT; "
                    'if "$inference_retirement_confirmed"; then /bin/rm -f -- "$NATIVE_IO_SENTINEL"; fi; '
                    'printf "%s %s %s\\n" "$status" "$inference_cancellation_status" "$inference_retirement_confirmed" > "$NATIVE_IO_RETURN"; '
                    'printf "%s" "$inference_diagnostic" > "$NATIVE_IO_DIAGNOSTIC"; exit "$status"\' EXIT; '
                    'if [[ "$NATIVE_IO_RESULT" == image-id ]]; then '
                    "inference_native probe untimed image-id bash -c 'printf \"sha256:%064d\\n\" 0'; "
                    'else inference_native probe untimed discard "$2" "$3" native-client; fi'
                )
                environment = self.environment | {
                    "NATIVE_STATUS": native_status,
                    "NATIVE_IO_PHASE": phase,
                    "NATIVE_IO_RESULT": result,
                    "NATIVE_IO_ENTERED": str(entered),
                    "NATIVE_IO_RETURN": str(receipt),
                    "NATIVE_IO_DIAGNOSTIC": str(diagnostic),
                    "NATIVE_IO_SENTINEL": str(sentinel),
                    "NATIVE_IO_ANCHOR": str(failed_anchor),
                    f"BASH_FUNC_{utility}%%": body,
                }
                read_descriptor, write_descriptor = os.pipe()
                process = None
                try:
                    os.set_blocking(write_descriptor, False)
                    prefill = b""
                    while True:
                        try:
                            count = os.write(write_descriptor, b"x" * 4096)
                            prefill += b"x" * count
                        except BlockingIOError:
                            break
                    os.set_blocking(write_descriptor, True)
                    process = subprocess.Popen(
                        ["/bin/bash", "-euc", script, "bash", str(self.helper), sys.executable, str(Path(__file__).resolve())],
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=write_descriptor,
                        start_new_session=True,
                    )
                    os.close(write_descriptor)
                    write_descriptor = None
                    self.assertEqual(expected_status, process.wait(timeout=15))
                    self.assertEqual(phase + "\n", entered.read_text())
                    self.assertEqual(f"{expected_status} 0 {expected_fact}\n", receipt.read_text())
                    payload = diagnostic.read_text()
                    self.retain_cleanup(payload)
                    self.assertIn(reason, payload)
                    self.assertEqual(expected_fact == "false", sentinel.exists())
                    if failed_anchor.exists():
                        self.assert_retired(int(failed_anchor.read_text()))
                    if self.observation.exists():
                        self.assert_client_retired()
                    stdout, _ = process.communicate(timeout=3)
                    self.assertEqual(b"", stdout)
                    os.set_blocking(read_descriptor, False)
                    drained = b""
                    while True:
                        chunk = os.read(read_descriptor, 65536)
                        if not chunk:
                            break
                        drained += chunk
                    self.assertEqual(prefill, drained, msg="Control I/O wrote to public stderr before return")
                finally:
                    if write_descriptor is not None:
                        os.close(write_descriptor)
                    # Closing only the owned reader releases a failed finite
                    # fault probe; no unobserved process-group rescue is used.
                    os.close(read_descriptor)
                    if process is not None:
                        process.communicate(timeout=15)

    def test_captured_descriptor_setup_restores_following_stdio_and_caller_stderr(self):
        script = (
            'source "$1"; inference_native captured untimed discard bash -c \'printf "captured\\000\\377" >&2\'; '
            '[[ "$inference_retirement_confirmed" == true && -z "$inference_diagnostic" ]]; '
            "inference_native streaming untimed stdio bash -c 'printf \"stdio\\000\\377\\n\" >&2'; "
            '[[ "$inference_retirement_confirmed" == true && -z "$inference_diagnostic" ]]; '
            'printf "caller\\000\\376\\n" >&2'
        )
        result = subprocess.run(
            ["/bin/bash", "-euc", script, "bash", str(self.helper)],
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"stdio\0\xff\ncaller\0\xfe\n", result.stderr)

    # silent-defaults: allow-contract -- Omitted result mode selects this fixture's captured discard profile; streaming scenarios select stdio explicitly; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_container_ordinary_signal_number_status_is_not_cancellation_or_expiry
    def container_policy_command(self, *, budget, mode="discard"):
        command, environment = self.command(budget=budget)
        command[2] = (
            'source "$1"; '
            "sleep() { "
            'if [[ "$#" == 1 && "$1" == 2 ]]; then '
            'printf "%s\\n" "$1" >> "$NATIVE_GRACE_CALLS"; '
            'if [[ -f "$scratch/status.deadline" && ! -L "$scratch/status.deadline" ]]; then '
            '/bin/cp -- "$scratch/status.deadline" "$NATIVE_DEADLINE_OBSERVED"; '
            'else printf absent > "$NATIVE_DEADLINE_ABSENT"; fi; fi; command sleep "$@"; }; '
            "trap 'status=$?; trap - EXIT; "
            'printf "%s %s %s\\n" "$status" "$inference_cancellation_status" "$inference_retirement_confirmed" > "$NATIVE_OUTCOME"; '
            'printf "%s" "$inference_diagnostic" >&2; exit "$status"\' EXIT; '
            'container_native probe "$2" "$NATIVE_RESULT" "$3" "$4" native-client'
        )
        return command, environment | {
            "NATIVE_RESULT": mode,
            "NATIVE_BEHAVIOR": "original-signals",
            "NATIVE_OUTCOME": str(self.root / "container-outcome"),
            "NATIVE_GRACE_CALLS": str(self.root / "container-grace"),
            "NATIVE_DEADLINE_OBSERVED": str(self.root / "container-deadline"),
            "NATIVE_DEADLINE_ABSENT": str(self.root / "container-no-deadline"),
            "NATIVE_SIGNAL_MARKER": str(self.root / "signal-observed"),
        }

    def reset_container_observations(self):
        for name in (
            "client.json",
            "control.json",
            "container-handlers-ready",
            "signal-observed",
            "container-outcome",
            "container-grace",
            "container-deadline",
            "container-no-deadline",
        ):
            (self.root / name).unlink(missing_ok=True)

    def test_container_numeric_ten_second_expiry_uses_real_timeout_and_two_second_grace(self):
        command, environment = self.container_policy_command(budget="10")
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.retire_probe, process)
        self.await_file(self.root / "container-handlers-ready")
        observed = json.loads(self.observation.read_text())
        wrapper = Path("/proc", str(observed["parent"]))
        timeout_pid = int((wrapper / "stat").read_text().rpartition(")")[2].split()[1])
        timeout_process = Path("/proc", str(timeout_pid))
        arguments = (timeout_process / "cmdline").read_bytes().split(b"\0")[:-1]
        self.assertEqual([b"timeout", b"--foreground", b"--signal=USR2", b"--kill-after=2", b"10"], arguments[:5])
        executable = (timeout_process / "exe").resolve(strict=True)
        self.assertEqual(Path("/usr/bin/timeout").resolve(), executable)
        version = subprocess.run([str(executable), "--version"], capture_output=True, text=True, check=True)
        self.assertIn("GNU coreutils", version.stdout.splitlines()[0])
        self.await_grace_sleep(process, seconds=2, wait_seconds=13)
        self.assertEqual(b"expired\n", (self.root / "container-deadline").read_bytes())
        self.assertTrue(Path("/proc", str(observed["descendant"])).exists())
        self.await_file(self.root / "signal-observed")
        self.assertEqual("TERM\n", (self.root / "signal-observed").read_text())
        stdout, stderr = process.communicate(timeout=5)
        self.retain_cleanup(stderr)
        self.assertEqual(124, process.returncode, msg=stderr)
        self.assertEqual("", stdout)
        self.assertIn("deadline elapsed", stderr)
        self.assertEqual("124 0 true\n", (self.root / "container-outcome").read_text())
        self.assertEqual(["2"], (self.root / "container-grace").read_text().splitlines())
        self.assertFalse((self.root / "container-no-deadline").exists())
        self.assert_client_retired()

    def test_container_original_signals_reach_numeric_and_untimed_clients_without_expiry(self):
        for budget, mode in (("10", "discard"), ("untimed", "stdio")):
            for signum, name in ((signal.SIGHUP, "HUP"), (signal.SIGINT, "INT"), (signal.SIGTERM, "TERM")):
                with self.subTest(budget=budget, signal=signum):
                    self.reset_container_observations()
                    command, environment = self.container_policy_command(budget=budget, mode=mode)
                    process = subprocess.Popen(
                        command,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                    self.addCleanup(self.retire_probe, process)
                    self.await_file(self.root / "container-handlers-ready")
                    observed = json.loads(self.observation.read_text())
                    process.send_signal(signum)
                    self.await_grace_sleep(process, seconds=2)
                    self.assertTrue(Path("/proc", str(observed["descendant"])).exists())
                    stdout, stderr = process.communicate(timeout=5)
                    self.retain_cleanup(stderr)
                    expected = 128 + signum
                    self.assertEqual(expected, process.returncode, msg=stderr)
                    self.assertEqual("", stdout)
                    self.assertEqual(name + "\n", (self.root / "signal-observed").read_text())
                    self.assertEqual(f"{expected} {expected} true\n", (self.root / "container-outcome").read_text())
                    self.assertEqual(["2"], (self.root / "container-grace").read_text().splitlines())
                    self.assertEqual(b"absent", (self.root / "container-no-deadline").read_bytes())
                    self.assertFalse((self.root / "container-deadline").exists())
                    self.assertNotIn("deadline elapsed", stderr)
                    self.assert_client_retired()

    def test_container_expiry_and_repeated_cancellation_never_renew_grace(self):
        original = self.helper.read_text()
        guard = "                        if [[ $cancelled == 0 ]]; then\n"
        self.assertEqual(1, original.count(guard))
        for phase in ("known-at-expiry", "during-expiry", "during-cancellation"):
            with self.subTest(phase=phase):
                self.reset_container_observations()
                self.helper.write_text(original)
                if phase == "known-at-expiry":
                    # A real signal at the exact validated-expiry guard keeps
                    # timeout and expired bytes real while fixing this ordering.
                    wait_for_term = (
                        "                        marker_deadline=$((SECONDS + 3))\n"
                        '                        while ! grep -Fxq TERM "$NATIVE_SIGNAL_MARKER" 2>/dev/null '
                        "&& (( SECONDS < marker_deadline )); do command sleep 0.01; done\n"
                        '                        if grep -Fxq TERM "$NATIVE_SIGNAL_MARKER" 2>/dev/null; '
                        'then kill -HUP "$BASHPID"; fi\n'
                    )
                    self.helper.write_text(original.replace(guard, wait_for_term + guard, 1))
                # silent-defaults: allow-contract -- The cancellation-only scenario is untimed; the two expiry scenarios receive a one-second client budget; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_container_expiry_and_repeated_cancellation_never_renew_grace
                budget = "untimed" if phase == "during-cancellation" else "1"
                command, environment = self.container_policy_command(budget=budget, mode="stdio")
                process = subprocess.Popen(
                    command,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(self.root / "container-handlers-ready")
                if phase == "during-cancellation":
                    process.send_signal(signal.SIGTERM)
                grace = self.await_grace_sleep(process, seconds=2, wait_seconds=5)
                if phase != "known-at-expiry":
                    process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=5)
                self.retain_cleanup(stderr)
                # silent-defaults: allow-contract -- The known-expiry case injects HUP and expects 129; the other cases inject INT and expect 130; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_container_expiry_and_repeated_cancellation_never_renew_grace
                expected = 129 if phase == "known-at-expiry" else 130
                self.assertEqual(expected, process.returncode, msg=stderr)
                self.assertEqual("", stdout)
                self.assertEqual(f"{expected} {expected} true\n", (self.root / "container-outcome").read_text())
                self.assertEqual(["2"], (self.root / "container-grace").read_text().splitlines())
                markers = (self.root / "signal-observed").read_text().splitlines()
                if phase == "during-cancellation":
                    self.assertEqual(["TERM"], markers)
                    self.assertFalse((self.root / "container-deadline").exists())
                else:
                    self.assertEqual(b"expired\n", (self.root / "container-deadline").read_bytes())
                    self.assertIn("TERM", markers)
                    if phase == "known-at-expiry":
                        self.assertIn("HUP", markers)
                    # Once expiry grace has elapsed, forwarding is followed
                    # by KILL; a late handler is not promised another grace.
                self.assert_retired(grace)
                self.assert_client_retired()
        self.helper.write_text(original)

    def test_container_ordinary_signal_number_status_is_not_cancellation_or_expiry(self):
        for status in (129, 130, 143):
            with self.subTest(status=status):
                self.reset_container_observations()
                command, environment = self.container_policy_command(budget="10")
                result = subprocess.run(
                    command,
                    env=environment | {"NATIVE_BEHAVIOR": "", "NATIVE_STATUS": str(status)},
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.retain_cleanup(result.stderr)
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual(f"{status} 0 true\n", (self.root / "container-outcome").read_text())
                self.assertIn(f"native client exited with status {status}", result.stderr)
                self.assertFalse((self.root / "container-grace").exists())
                self.assertFalse((self.root / "container-deadline").exists())
                self.assert_client_retired()

    # silent-defaults: allow-contract -- Direct captured-command probes use the declared twenty-second client budget unless a scenario supplies another policy; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_cleanup_preserves_primary_and_traps_are_restored
    def command(self, *, budget="20", environment=None):
        return [
            "/bin/bash",
            "-euc",
            'source "$1"; inference_diagnostic=; '
            'trap \'status=$?; trap - EXIT; printf "%s" "$inference_diagnostic" >&2; exit "$status"\' EXIT; '
            'inference_native probe "$2" discard "$3" "$4" native-client',
            "bash",
            str(self.helper),
            budget,
            sys.executable,
            str(Path(__file__).resolve()),
        ], (self.environment if environment is None else environment)

    # silent-defaults: allow-contract -- Ordinary probes use a five-second test watchdog and twenty-second native budget; timing-specific cases explicitly select their own bounds; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_cleanup_preserves_primary_and_traps_are_restored
    def run_native(self, *, budget="20", additions=None, watchdog_seconds=5):
        command, environment = self.command(budget=budget)
        # silent-defaults: allow-contract -- Omitted additions preserve the declared client scenario; supplied entries overlay only their selected inputs; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_cli_signal_dispositions_are_native
        result = subprocess.run(
            command,
            env=environment | ({} if additions is None else additions),
            text=True,
            capture_output=True,
            timeout=watchdog_seconds,
            check=False,
        )
        self.retain_cleanup(result.stderr)
        return result

    def retain_cleanup(self, stderr):
        for name in re.findall(r"/tmp/nmrnet-inference-native\.[A-Za-z0-9]+", stderr):
            self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", name], check=True)

    def assert_retired(self, identifier):
        process = Path("/proc", str(identifier))
        deadline = time.monotonic() + 2
        while process.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(process.exists(), msg=f"owned fixture process {identifier} remained")

    def assert_client_retired(self):
        observed = json.loads(self.observation.read_text())
        self.assert_retired(observed["pid"])
        if "descendant" in observed:
            self.assert_retired(observed["descendant"])

    def fact_command(self, *, budget, additions=None):
        command, environment = self.command(budget=budget)
        command[2] = (
            'source "$1"; inference_diagnostic=; inference_retirement_confirmed=false; '
            'trap \'status=$?; trap - EXIT; printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_FACT"; '
            'printf "%s" "$inference_diagnostic" >&2; exit "$status"\' EXIT; '
            'if inference_native probe "$2" discard "$3" "$4" native-client; then status=0; else status=$?; fi; exit "$status"'
        )
        environment = environment | {"NATIVE_FACT": str(self.root / "retired")}
        if additions is not None:
            environment |= additions
        return command, environment

    def run_fact(self, *, budget, additions=None):
        command, environment = self.fact_command(budget=budget, additions=additions)
        result = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=5, check=False)
        self.retain_cleanup(result.stderr)
        return result, (self.root / "retired").read_text().strip()

    def outcome_command(self, result, *, next_success):
        command, environment = self.command(budget="untimed")
        command[2] = (
            'source "$1"; inference_cancellation_status=777; '
            'if inference_native probe "$2" "$NATIVE_RESULT" "$3" "$4" native-client; then primary=0; else primary=$?; fi; '
            'printf "%s %s %s\\n" "$primary" "$inference_cancellation_status" "$inference_retirement_confirmed" > "$NATIVE_OUTCOME"; '
            'printf "%s" "$inference_diagnostic" >&2; '
            'if [[ "$NATIVE_NEXT" == yes ]]; then '
            'inference_native next untimed "$NATIVE_RESULT" true; '
            'printf "%s %s %s\\n" "$?" "$inference_cancellation_status" "$inference_retirement_confirmed" >> "$NATIVE_OUTCOME"; '
            'fi; exit "$primary"'
        )
        # silent-defaults: allow-contract -- The required next_success scenario selects whether a second successful native call exercises per-invocation reset; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_actual_signal_outcome_and_next_success_reset_are_per_invocation
        return command, environment | {
            "NATIVE_RESULT": result,
            "NATIVE_NEXT": "yes" if next_success else "no",
            "NATIVE_OUTCOME": str(self.root / "outcome"),
        }

    def test_ordinary_signal_number_status_has_no_actual_cancellation_outcome(self):
        for mode in ("discard", "stdio"):
            for status in (129, 130, 143):
                with self.subTest(mode=mode, status=status):
                    command, environment = self.outcome_command(mode, next_success=False)
                    result = subprocess.run(
                        command,
                        env=environment | {"NATIVE_STATUS": str(status)},
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    self.retain_cleanup(result.stderr)
                    self.assertEqual(status, result.returncode, msg=result.stderr)
                    self.assertEqual([f"{status} 0 true"], (self.root / "outcome").read_text().splitlines())
                    if mode == "discard":
                        self.assertIn(f"native client exited with status {status}", result.stderr)
                    self.assert_client_retired()

    def await_term_handler(self):
        self.await_file(self.observation)
        observed = json.loads(self.observation.read_text())
        status = Path("/proc", str(observed["pid"]), "status")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
            if int(fields["SigCgt"].strip(), 16) & (1 << (signal.SIGTERM - 1)):
                return observed
            time.sleep(0.01)
        raise self.failureException("The actual native client did not install its TERM handler.")

    # silent-defaults: allow-contract -- Observe the actual one-second untimed grace within a three-second fixture window; container-grace cases supply their own bounds; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_second_signal_during_untimed_grace_updates_final_cancellation
    def await_grace_sleep(self, process, *, seconds=1, wait_seconds=3):
        children = Path("/proc", str(process.pid), "task", str(process.pid), "children")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), msg="The owner exited before its grace phase was observed.")
            for identifier in children.read_text().split():
                child = Path("/proc", identifier)
                try:
                    arguments = (child / "cmdline").read_bytes().split(b"\0")
                    waiting = (child / "wchan").read_text().strip()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if arguments == [b"sleep", str(seconds).encode(), b""] and waiting in {"hrtimer_nanosleep", "do_nanosleep"}:
                    return int(identifier)
            time.sleep(0.01)
        raise self.failureException(f"The actual owner did not enter its {seconds}-second grace sleep.")

    def test_untimed_cancellation_offers_term_before_retiring_resistant_descendant(self):
        for mode in ("discard", "stdio"):
            with self.subTest(mode=mode):
                self.observation.unlink(missing_ok=True)
                marker = self.root / "term-observed"
                marker.unlink(missing_ok=True)
                command, environment = self.outcome_command(mode, next_success=False)
                process = subprocess.Popen(
                    command,
                    env=environment | {"NATIVE_BEHAVIOR": "resistant"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                self.addCleanup(self.retire_probe, process)
                self.await_term_handler()
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(143, process.returncode, msg=stderr)
                self.assertEqual("", stdout)
                self.assertEqual("TERM\n", marker.read_text())
                self.assertEqual(["143 143 true"], (self.root / "outcome").read_text().splitlines())
                self.assert_client_retired()

    def test_numeric_cancellation_does_not_forward_term_or_invent_expiry(self):
        command, environment = self.outcome_command("discard", next_success=False)
        self.assertEqual("untimed", command[5])
        command[5] = "20"
        process = subprocess.Popen(
            command,
            env=environment | {"NATIVE_BEHAVIOR": "resistant"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.retire_probe, process)
        self.await_term_handler()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=4)
        self.retain_cleanup(stderr)
        self.assertEqual(143, process.returncode, msg=stderr)
        self.assertEqual("", stdout)
        self.assertFalse((self.root / "term-observed").exists())
        self.assertNotIn("deadline elapsed", stderr)
        self.assertEqual(["143 143 true"], (self.root / "outcome").read_text().splitlines())
        self.assert_client_retired()

    def test_second_signal_during_untimed_grace_updates_final_cancellation(self):
        command, environment = self.outcome_command("stdio", next_success=False)
        process = subprocess.Popen(
            command,
            env=environment | {"NATIVE_BEHAVIOR": "resistant"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.retire_probe, process)
        self.await_term_handler()
        process.send_signal(signal.SIGTERM)
        self.await_file(self.root / "term-observed")
        grace = self.await_grace_sleep(process)
        anchor = json.loads(self.control.read_text())["anchor"]
        self.assertTrue(Path("/proc", str(anchor)).exists())
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=4)
        self.retain_cleanup(stderr)
        self.assertEqual(130, process.returncode, msg=stderr)
        self.assertEqual("", stdout)
        self.assertEqual(["130 130 true"], (self.root / "outcome").read_text().splitlines())
        self.assertEqual("TERM\n", (self.root / "term-observed").read_text())
        self.assert_retired(grace)
        self.assert_retired(anchor)
        self.assert_client_retired()

    def test_actual_signal_outcome_and_next_success_reset_are_per_invocation(self):
        for mode in ("discard", "stdio"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(mode=mode, signum=signum):
                    self.observation.unlink(missing_ok=True)
                    command, environment = self.outcome_command(mode, next_success=True)
                    process = subprocess.Popen(
                        command,
                        env=environment | {"NATIVE_BEHAVIOR": "resistant"},
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                    self.addCleanup(self.retire_probe, process)
                    self.await_file(self.observation)
                    process.send_signal(signum)
                    _, stderr = process.communicate(timeout=5)
                    self.retain_cleanup(stderr)
                    status = 128 + signum
                    self.assertEqual(status, process.returncode, msg=stderr)
                    self.assertEqual([f"{status} {status} true", "0 0 true"], (self.root / "outcome").read_text().splitlines())
                    self.assert_client_retired()

    def test_early_known_and_unknown_failures_reset_outcome_without_dispatch(self):
        source = self.helper.read_text()
        creation = "scratch=$(umask 077; mktemp -d /tmp/nmrnet-inference-native.XXXXXXXX 2>/dev/null)"
        self.assertEqual(1, source.count(creation))
        for mode in ("discard", "stdio"):
            for early, status, fact in ((True, 28, "true"), (False, 1, "false")):
                with self.subTest(mode=mode, early=early):
                    self.helper.write_text(source.replace(creation, "scratch=$(exit 28)", 1) if early else source)
                    command, environment = self.outcome_command(mode, next_success=False)
                    # silent-defaults: allow-contract -- Scratch creation failure needs no publication fault; the post-creation case fails ready publication before native dispatch; proof=tests/repository/test_inference_native.py::CapturedNativeTests.test_early_known_and_unknown_failures_reset_outcome_without_dispatch
                    additions = {} if early else {"NATIVE_PUBLICATION_FAILURE": "ready"}
                    result = subprocess.run(
                        command, env=environment | additions, capture_output=True, text=True, timeout=5, check=False
                    )
                    self.retain_cleanup(result.stderr)
                    self.assertEqual(status, result.returncode, msg=result.stderr)
                    self.assertEqual([f"{status} 0 {fact}"], (self.root / "outcome").read_text().splitlines())
                    self.assertFalse(self.observation.exists())
                    if not early:
                        self.assert_retired(json.loads(self.control.read_text())["anchor"])

    def test_final_snapshot_reconciles_signal_after_existing_status_reconciliation(self):
        source = self.helper.read_text()
        boundary = "    trap - HUP INT TERM USR1\n"
        self.assertEqual(1, source.count(boundary))
        for mode in ("discard", "stdio"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(mode=mode, signum=signum):
                    # Inject at the unique copy boundary; this does not claim
                    # to observe the external scheduler's tiny signal window.
                    self.helper.write_text(source.replace(boundary, f'    kill -{int(signum)} "$BASHPID"\n' + boundary, 1))
                    command, environment = self.outcome_command(mode, next_success=False)
                    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=5, check=False)
                    self.retain_cleanup(result.stderr)
                    status = 128 + signum
                    self.assertEqual(status, result.returncode, msg=result.stderr)
                    self.assertEqual([f"{status} {status} true"], (self.root / "outcome").read_text().splitlines())
                    self.assert_client_retired()

    def test_untimed_completion_preserves_status_and_has_no_short_operation_deadline(self):
        result, fact = self.run_fact(budget="0.2", additions={"NATIVE_BEHAVIOR": "slow-success"})
        self.assertEqual(124, result.returncode, msg=result.stderr)
        self.assertEqual("true", fact)
        for status in (0, 7, 137):
            with self.subTest(status=status):
                result, fact = self.run_fact(
                    budget="untimed", additions={"NATIVE_BEHAVIOR": "slow-success", "NATIVE_STATUS": str(status)}
                )
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual("true", fact)
                self.assertNotIn("deadline elapsed", result.stderr)
                self.assert_client_retired()

    def test_untimed_output_eof_and_held_descendant_retire_before_fact_is_true(self):
        for behavior in ("closed-output", "held-descendant"):
            with self.subTest(behavior=behavior):
                result, fact = self.run_fact(budget="untimed", additions={"NATIVE_BEHAVIOR": behavior, "NATIVE_STATUS": "7"})
                self.assertEqual(7, result.returncode, msg=result.stderr)
                self.assertEqual("true", fact)
                self.assertEqual("", result.stdout)
                self.assert_client_retired()

    def test_untimed_cancellation_preserves_signals_and_confirms_retirement(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.observation.unlink(missing_ok=True)
                command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "resistant"})
                process = subprocess.Popen(
                    command, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(self.observation)
                process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assertEqual("true\n", (self.root / "retired").read_text())
                self.assertEqual("", stdout)
                self.assert_client_retired()

    def test_retirement_fact_is_false_during_the_birth_handoff(self):
        source = self.helper.read_text()
        birth = "        (\n            exec {capture_guard}>&-\n            umask 077\n"
        self.assertEqual(1, source.count(birth))
        sample = '            printf "%s\\n" "$inference_retirement_confirmed" "$BASHPID" > "$NATIVE_BIRTH_FACT"\n'
        source = source.replace(birth, "        (\n" + sample + birth[len("        (\n") :], 1)
        capture = "    drain=$!\n"
        self.assertEqual(1, source.count(capture))
        receipt = self.root / "birth-fact"
        self.helper.write_text(source.replace(capture, "    sleep 0.25\n" + capture, 1))
        result, fact = self.run_fact(budget="untimed", additions={"NATIVE_BIRTH_FACT": str(receipt)})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        sampled, child = receipt.read_text().splitlines()
        self.assertEqual("false", sampled)
        self.assert_retired(int(child))
        self.assertEqual("true", fact)

    def test_admitted_anchor_loss_retains_caller_resource_and_stops(self):
        drain_receipt = self.root / "drain"
        sentinel = self.root / "caller-resource"
        sentinel.write_text("still owned")
        source = self.helper.read_text()
        capture = "    drain=$!\n"
        self.assertEqual(1, source.count(capture))
        self.helper.write_text(source.replace(capture, capture + '    printf "%s\\n" "$drain" > "$NATIVE_DRAIN"\n', 1))
        command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "resistant"})
        command[2] = (
            'source "$1"; inference_diagnostic=; inference_retirement_confirmed=false; '
            'trap \'status=$?; trap - EXIT; if "$inference_retirement_confirmed"; then /bin/rm -f "$NATIVE_SENTINEL"; fi; '
            'printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_FACT"; '
            'printf "%s" "$inference_diagnostic" >&2; exit "$status"\' EXIT; '
            'if inference_native uncertain "$2" discard "$3" "$4" native-client; then status=0; else status=$?; fi; '
            'if ! "$inference_retirement_confirmed"; then exit "$status"; fi; '
            'printf forbidden > "$NATIVE_SUBSEQUENT"; inference_native forbidden 5 discard true'
        )
        process = subprocess.Popen(
            command,
            env=environment
            | {
                "NATIVE_DRAIN": str(drain_receipt),
                "NATIVE_SENTINEL": str(sentinel),
                "NATIVE_SUBSEQUENT": str(self.root / "next"),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self.retire_probe, process)
        self.await_file(self.observation)
        observed = json.loads(self.observation.read_text())
        drain = int(drain_receipt.read_text())
        # These finite fixture processes remain recorded after their anchor dies.
        # Cleanup uses only those exact observations, after collecting primary status.
        os.kill(observed["parent"], signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=4)
            self.retain_cleanup(stderr)
            self.assertEqual(1, process.returncode, msg=stderr)
            self.assertEqual("", stdout)
            self.assertEqual("false\n", (self.root / "retired").read_text())
            self.assertTrue(sentinel.exists())
            self.assertFalse((self.root / "next").exists())
            self.assertIn("Cannot confirm native client retirement", stderr)
            self.assertTrue(Path("/proc", str(observed["descendant"])).exists())
        finally:
            for identifier in (observed["pid"], observed["descendant"], drain):
                self.kill_recorded_fixture(identifier)

    def kill_recorded_fixture(self, identifier):
        with suppress(ProcessLookupError):
            os.kill(identifier, signal.SIGKILL)
        self.assert_retired(identifier)

    def test_caller_cleanup_reads_confirmed_fact_at_return_signal_handoff(self):
        source = self.helper.read_text()
        handoff = (
            '    if "$monitor_enabled"; then set -m; fi\n'
            "    inference_cancellation_status=$cancelled\n"
            "    if [[ $inference_cancellation_status != 0 ]]; then status=$inference_cancellation_status; fi\n"
            '    return "$status"\n'
        )
        self.assertEqual(1, source.count(handoff))
        for signum, expected in ((None, 7), (signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signum):
                sentinel = self.root / "caller-resource"
                sentinel.write_text("owned")
                self.helper.write_text(source)
                if signum is not None:
                    self.helper.write_text(source.replace(handoff, f'    kill -{signum.value} "$BASHPID"\n' + handoff, 1))
                command, environment = self.fact_command(budget="untimed", additions={"NATIVE_STATUS": "7"})
                command[2] = (
                    'source "$1"; inference_diagnostic=; inference_retirement_confirmed=false; '
                    'trap \'status=$?; trap - EXIT; if "$inference_retirement_confirmed"; then /bin/rm -f "$NATIVE_SENTINEL"; fi; '
                    'printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_FACT"; '
                    'printf "%s" "$inference_diagnostic" >&2; exit "$status"\' EXIT; '
                    "trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM; "
                    'if inference_native probe "$2" discard "$3" "$4" native-client; then status=0; else status=$?; fi; exit "$status"'
                )
                result = subprocess.run(
                    command,
                    env=environment | {"NATIVE_SENTINEL": str(sentinel)},
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.retain_cleanup(result.stderr)
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertEqual("true\n", (self.root / "retired").read_text())
                self.assertFalse(sentinel.exists())
                self.assert_client_retired()

    def test_anchor_join_interruption_retries_before_confirming_retirement(self):
        source = self.helper.read_text()
        retirement = 'kill -KILL -- "-$leader"'
        join = '            wait "$leader" 2>/dev/null || :\n'
        self.assertEqual(1, source.count(retirement))
        self.assertEqual(1, source.count(join))
        # TERM leaves the held anchor live so its actual wait can be interrupted.
        # The external probe releases that same held group after observing retry.
        source = source.replace(retirement, 'kill -TERM -- "-$leader"', 1)
        source = source.replace(
            join,
            '            printf "%s %s\\n" "$interrupt_count" "$inference_retirement_confirmed" >> "$NATIVE_JOIN"\n' + join,
            1,
        )
        self.helper.write_text(source)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.observation.unlink(missing_ok=True)
                receipt = self.root / "join"
                receipt.unlink(missing_ok=True)
                command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "held-descendant"})
                process = subprocess.Popen(
                    command,
                    env=environment | {"NATIVE_JOIN": str(receipt)},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(receipt)
                observed = json.loads(self.observation.read_text())
                try:
                    process.send_signal(signum)
                    deadline = time.monotonic() + 2
                    while len(receipt.read_text().splitlines()) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(["0 false", "1 false"], receipt.read_text().splitlines())
                finally:
                    os.killpg(observed["parent"], signal.SIGKILL)
                _, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assertEqual("true\n", (self.root / "retired").read_text())
                self.assert_client_retired()

    def test_retirement_fact_waits_for_anchor_and_drain_joins(self):
        source = self.helper.read_text()
        join = '            if wait "$drain" 2>/dev/null; then drain_status=0; else drain_status=$?; fi\n'
        self.assertEqual(1, source.count(join))
        receipt = self.root / "before-join"
        delayed = '            printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_JOIN_FACT"\n' + join
        self.helper.write_text(source.replace(join, delayed, 1))
        result, fact = self.run_fact(
            budget="untimed", additions={"NATIVE_JOIN_FACT": str(receipt), "NATIVE_BEHAVIOR": "held-descendant"}
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("false\n", receipt.read_text())
        self.assertEqual("true", fact)
        self.assert_client_retired()

    def test_setup_before_birth_and_post_retirement_cleanup_have_separate_facts(self):
        source = self.helper.read_text()
        creation = "scratch=$(umask 077; mktemp -d /tmp/nmrnet-inference-native.XXXXXXXX 2>/dev/null)"
        self.assertEqual(1, source.count(creation))
        self.helper.write_text(source.replace(creation, "scratch=$(exit 28)", 1))
        result, fact = self.run_fact(budget="untimed")
        self.assertEqual(28, result.returncode, msg=result.stderr)
        self.assertEqual("true", fact)
        self.assertFalse(self.observation.exists())
        self.helper.write_text(source)
        for status, expected in (("0", 23), ("7", 7)):
            with self.subTest(status=status):
                result, fact = self.run_fact(budget="untimed", additions={"NATIVE_STATUS": status, "NATIVE_CLEANUP_FAILURE": "1"})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertEqual("true", fact)
                self.assert_client_retired()

    def test_caller_stops_and_retains_resources_after_unknown_retirement(self):
        sentinel = self.root / "caller-resource"
        sentinel.write_text("still owned")
        command, environment = self.fact_command(budget="untimed", additions={"NATIVE_CORRUPT_READY": "1"})
        command[2] = (
            'source "$1"; inference_diagnostic=; inference_retirement_confirmed=false; '
            'trap \'status=$?; trap - EXIT; if "$inference_retirement_confirmed"; then /bin/rm -f "$NATIVE_SENTINEL"; fi; '
            'printf "%s" "$inference_diagnostic" >&2; exit "$status"\' EXIT; '
            "inference_native prior 5 discard true; [[ $inference_retirement_confirmed == true ]]; "
            'if inference_native uncertain "$2" discard "$3" "$4" native-client; then status=0; else status=$?; fi; '
            'if ! "$inference_retirement_confirmed"; then exit "$status"; fi; '
            'printf "%s\\n" forbidden > "$NATIVE_SUBSEQUENT"; inference_native forbidden 5 discard true'
        )
        # Corrupt only the second call: the first true fact cannot grant later cleanup.
        source = self.helper.read_text()
        entry = "    inference_result=\n    inference_retirement_confirmed=true\n"
        self.assertEqual(1, source.count(entry))
        self.helper.write_text(
            source.replace(
                entry,
                entry
                + "    if [[ $operation == prior ]]; then unset NATIVE_CORRUPT_READY; else export NATIVE_CORRUPT_READY=1; fi\n",
                1,
            )
        )
        result = subprocess.run(
            command,
            env=environment | {"NATIVE_SENTINEL": str(sentinel), "NATIVE_SUBSEQUENT": str(self.root / "subsequent")},
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.retain_cleanup(result.stderr)
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertTrue(sentinel.exists())
        self.assertFalse((self.root / "subsequent").exists())
        observed = json.loads(self.control.read_text())
        os.killpg(observed["anchor"], signal.SIGKILL)
        self.assert_retired(observed["anchor"])

    def test_receipt_faults_preserve_status_while_retirement_fact_remains_true(self):
        for additions, expected in (
            ({"NATIVE_PUBLICATION_FAILURE": "status"}, 1),
            ({"NATIVE_STATUS_MUTATION": "read-error"}, 1),
            ({"NATIVE_STATUS_MUTATION": "fifo"}, 1),
            ({"NATIVE_DRAIN_FAILURE": "1"}, 33),
        ):
            with self.subTest(additions=additions):
                result, fact = self.run_fact(budget="untimed", additions=additions | {"NATIVE_BEHAVIOR": "held-descendant"})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertEqual("true", fact)
                self.assert_client_retired()

    def test_real_native_137_is_distinct_from_confirmed_deadline(self):
        result = self.run_native(additions={"NATIVE_STATUS": "137"})
        self.assertEqual(137, result.returncode, msg=result.stderr)
        self.assertIn("native client exited with status 137", result.stderr)
        self.assertNotIn("deadline elapsed", result.stderr)
        self.assert_client_retired()

    def test_resistant_expiry_forwards_term_and_retires_the_full_group(self):
        result = self.run_native(budget="0.1", additions={"NATIVE_BEHAVIOR": "resistant"})
        self.assertEqual(124, result.returncode, msg=result.stderr)
        self.assertIn("deadline elapsed", result.stderr)
        self.assertTrue((self.root / "term-observed").exists(), msg="the actual client never received deadline TERM")
        self.assert_client_retired()

    def test_late_success_after_confirmed_expiry_cannot_report_success(self):
        result = self.run_native(budget="0.1", additions={"NATIVE_BEHAVIOR": "late-success"})
        self.assertEqual(124, result.returncode, msg=result.stderr)
        self.assertIn("deadline elapsed", result.stderr)
        self.assert_client_retired()

    def test_cli_signal_dispositions_are_native(self):
        for budget in ("20", "untimed"):
            with self.subTest(budget=budget):
                result = self.run_native(budget=budget)
                self.assertEqual(0, result.returncode, msg=result.stderr)
                observed = json.loads(self.observation.read_text())
                self.assertFalse(observed["sigint_ignored"])
                self.assertFalse(observed["sigterm_ignored"])
                self.assertFalse(observed["sighup_ignored"])

    def test_output_eof_is_not_completion(self):
        started = time.monotonic()
        result = self.run_native(additions={"NATIVE_BEHAVIOR": "closed-output", "NATIVE_STATUS": "7"})
        self.assertEqual(7, result.returncode, msg=result.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assert_client_retired()

    def test_signal_cancellation_preserves_status_and_retires_resistant_descendant(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.observation.unlink(missing_ok=True)
                command, environment = self.command()
                process = subprocess.Popen(
                    command,
                    env=environment | {"NATIVE_BEHAVIOR": "resistant"},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(self.observation)
                process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assertEqual("", stdout)
                self.assert_client_retired()

    def await_file(self, path):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), msg=f"fixture did not publish {path.name}")

    def retire_probe(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

    def test_birth_cancellation_never_releases_native_dispatch(self):
        source = self.helper.read_text()
        capture = "    leader=$!\n"
        self.assertEqual(1, source.count(capture))
        delayed = '    printf "%s\\n" "$!" "$scratch" > "$NATIVE_BIRTH"\n    sleep 0.25\n' + capture
        self.helper.write_text(source.replace(capture, delayed, 1))
        birth = self.root / "birth"
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                birth.unlink(missing_ok=True)
                command, environment = self.command()
                process = subprocess.Popen(
                    command,
                    env=environment | {"NATIVE_BIRTH": str(birth)},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(birth)
                anchor, _ = birth.read_text().splitlines()
                process.send_signal(signum)
                _, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assert_retired(int(anchor))
                self.assertFalse(self.observation.exists(), msg="cancelled birth dispatched a native client")

    def test_failed_startup_with_no_writer_joins_the_drainer(self):
        source = self.helper.read_text()
        launch = '        exec bash -c "$anchor_program" bash "$scratch/status" "$owning_parent" "$budget" "$timed_program" "$deadline_signal" "$grace" "$@"'
        self.assertEqual(1, source.count(launch))
        source = source.replace(launch, "        exit 28", 1)
        source = source.replace('    ) 2>/dev/null > "$scratch/output" 2>&1 &', "    ) > /dev/null 2>&1 &", 1)
        source = source.replace("    drain=$!\n", '    drain=$!\n    printf "%s\\n" "$drain" > "$NATIVE_DRAIN"\n', 1)
        self.helper.write_text(source)
        receipt = self.root / "drain"
        result = self.run_native(additions={"NATIVE_DRAIN": str(receipt)})
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("native anchor did not publish", result.stderr)
        self.assert_retired(int(receipt.read_text()))
        self.assertFalse(self.observation.exists())

    def test_ready_or_release_publication_failure_blocks_dispatch(self):
        for phase, expected in (
            ("ready", 1),
            ("release", 29),
            ("release-fifo", 1),
            ("release-directory", 1),
            ("release-symlink", 1),
        ):
            with self.subTest(phase=phase):
                result = self.run_native(additions={"NATIVE_PUBLICATION_FAILURE": phase})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertFalse(self.observation.exists())
                self.assert_retired(json.loads(self.control.read_text())["anchor"])

    def test_corrupt_ready_receipt_retains_uncertain_state_without_dispatch(self):
        receipt = self.root / "drain"
        source = self.helper.read_text().replace(
            "    drain=$!\n",
            '    drain=$!\n    printf "%s\\n" "$drain" > "$NATIVE_DRAIN"\n',
            1,
        )
        self.helper.write_text(source)
        result = self.run_native(additions={"NATIVE_CORRUPT_READY": "1", "NATIVE_DRAIN": str(receipt)})
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("Cannot confirm native client retirement", result.stderr)
        self.assertFalse(self.observation.exists())
        observed = json.loads(self.control.read_text())
        self.assertTrue(Path(observed["directory"]).is_dir())
        # This external probe owns the finite injected anchor and retires it;
        # production correctly made no group effect from its corrupted receipt.
        os.killpg(observed["anchor"], signal.SIGKILL)
        self.assert_retired(observed["anchor"])
        self.assert_retired(int(receipt.read_text()))

    def test_terminal_publication_failure_is_prompt_and_retires_held_descendant(self):
        for phase in ("status", "printf"):
            with self.subTest(phase=phase):
                if phase == "printf":
                    source = self.helper.read_text()
                    terminal = '        if { printf "%s\\n" "$status" > "$receipt.pending" \\\n'
                    self.assertEqual(1, source.count(terminal))
                    self.helper.write_text(source.replace(terminal, "        if { false \\\n", 1))
                started = time.monotonic()
                result = self.run_native(additions={"NATIVE_PUBLICATION_FAILURE": phase, "NATIVE_BEHAVIOR": "held-descendant"})
                self.assertEqual(1, result.returncode, msg=result.stderr)
                self.assertLess(time.monotonic() - started, 3)
                self.assertIn("publication failed; the client status is unobserved", result.stderr)
                self.assert_client_retired()

    def test_status_read_and_byte_mutations_retire_before_cleanup(self):
        for mutation, expected in (
            ("read-error", 1),
            ("symlink", 1),
            ("fifo", 1),
            ("directory", 1),
            ("deadline-fifo", 1),
            ("deadline-directory", 1),
            ("deadline-symlink", 1),
            ("nul", 2),
            ("cr", 2),
            ("missing-lf", 2),
            ("extra-lf", 2),
            ("extra-line", 2),
            ("negative", 2),
            ("256", 2),
            ("text", 2),
        ):
            with self.subTest(mutation=mutation):
                result = self.run_native(additions={"NATIVE_STATUS_MUTATION": mutation, "NATIVE_BEHAVIOR": "held-descendant"})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                if mutation.startswith("deadline-"):
                    self.assertIn("native deadline receipt", result.stderr)
                else:
                    self.assertIn("native completion receipt", result.stderr)
                self.assert_client_retired()

    def test_failed_expiry_publication_cannot_claim_a_confirmed_deadline(self):
        result = self.run_native(
            budget="0.1", additions={"NATIVE_PUBLICATION_FAILURE": "deadline", "NATIVE_BEHAVIOR": "resistant"}
        )
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("publication failed", result.stderr)
        self.assertNotIn("deadline elapsed", result.stderr)
        self.assert_client_retired()

    def test_drain_failure_preserves_native_failure_and_rejects_success(self):
        for native_status, expected in (("0", 33), ("7", 7)):
            with self.subTest(status=native_status):
                # Status precedence permits ten seconds for startup and twenty
                # for native work, plus termination and fixture overhead.
                result = self.run_native(
                    additions={"NATIVE_STATUS": native_status, "NATIVE_DRAIN_FAILURE": "1"},
                    watchdog_seconds=35,
                )
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assert_client_retired()

    def test_cancellation_wins_over_terminal_publication_failure(self):
        result = self.run_native(
            additions={
                "NATIVE_PUBLICATION_FAILURE": "status",
                "NATIVE_CANCEL_PUBLICATION": "1",
                "NATIVE_BEHAVIOR": "held-descendant",
            }
        )
        self.assertEqual(143, result.returncode, msg=result.stderr)
        self.assertIn("interrupted", result.stderr)
        self.assert_client_retired()

    def test_cleanup_preserves_primary_and_traps_are_restored(self):
        for effect in ("1", "after-delete"):
            for native_status, expected in (("0", 23), ("7", 7)):
                with self.subTest(effect=effect, status=native_status):
                    result = self.run_native(additions={"NATIVE_STATUS": native_status, "NATIVE_CLEANUP_FAILURE": effect})
                    self.assertEqual(expected, result.returncode, msg=result.stderr)
                    if native_status == "0":
                        self.assertIn("Cannot confirm native client evidence directory removal; attempted path", result.stderr)
                        self.assertNotIn("retained", result.stderr)
                    else:
                        self.assertIn("Cannot remove native client", result.stderr)
                    scratch = Path(json.loads(self.control.read_text())["directory"])
                    self.assertEqual(effect == "1" or native_status == "7", scratch.exists())
                    self.assert_client_retired()
        command, environment = self.command()
        command[2] = (
            'trap ":" HUP INT TERM USR1; before=$(trap -p HUP INT TERM USR1); source "$1"; '
            "inference_diagnostic=; "
            'trap \'status=$?; trap - EXIT; printf "%s" "$inference_diagnostic" >&2; exit "$status"\' EXIT; '
            'inference_native probe "$2" discard true; [[ $(trap -p HUP INT TERM USR1) == "$before" ]]'
        )
        result = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=5, check=False)
        self.assertEqual(0, result.returncode, msg=result.stderr)

    def test_cleanup_cancellation_preserves_signal_status_and_actual_directory_effect(self):
        for effect in ("1", "after-delete"):
            for native_status in ("0", "7"):
                for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                    with self.subTest(effect=effect, status=native_status, signal=signum):
                        result, fact = self.run_fact(
                            budget="untimed",
                            additions={
                                "NATIVE_STATUS": native_status,
                                "NATIVE_CLEANUP_FAILURE": effect,
                                "NATIVE_CANCEL_CLEANUP": str(signum),
                            },
                        )
                        self.assertEqual(128 + signum, result.returncode, msg=result.stderr)
                        self.assertEqual("true", fact)
                        scratch = Path(json.loads(self.control.read_text())["directory"])
                        self.assertEqual(effect == "1" or native_status == "7", scratch.exists())
                        if native_status == "0":
                            self.assertIn("evidence directory removal; attempted path", result.stderr)
                            self.assertNotIn("retained", result.stderr)
                        self.assert_client_retired()

    def test_returned_diagnostic_is_private_and_resets_before_next_call(self):
        diagnostic = self.root / "diagnostic"
        reset = self.root / "reset"
        result = subprocess.run(
            [
                "/bin/bash",
                "-euc",
                'source "$1"; inference_diagnostic=poison; '
                'if inference_native probe untimed discard "$2" "$3" native-client; then primary=0; else primary=$?; fi; '
                'printf "%s" "$inference_diagnostic" > "$NATIVE_DIAGNOSTIC"; '
                "inference_native next untimed discard true; "
                'printf "%s" "$inference_diagnostic" > "$NATIVE_RESET"; exit "$primary"',
                "bash",
                str(self.helper),
                sys.executable,
                str(Path(__file__).resolve()),
            ],
            env=self.environment
            | {
                "NATIVE_STATUS": "7",
                "NATIVE_BEHAVIOR": "stream-finite",
                "NATIVE_DIAGNOSTIC": str(diagnostic),
                "NATIVE_RESET": str(reset),
            },
            capture_output=True,
            # Two ownership admissions may each take ten seconds. This is a
            # watchdog for both calls and retirement, not a latency assertion.
            timeout=25,
            check=False,
        )
        self.assertEqual(7, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)
        text = diagnostic.read_text()
        self.retain_cleanup(text)
        self.assertNotIn("poison", text)
        self.assertIn("native client exited with status 7", text)
        match = re.search(r"retained at (/tmp/nmrnet-inference-native\.[A-Za-z0-9]+/command\.log)", text)
        self.assertIsNotNone(match, msg=text)
        self.assertEqual(b"stdout\0\r\nstderr\0\r\n", Path(match.group(1)).read_bytes())
        self.assertEqual(b"", reset.read_bytes())
        self.assert_client_retired()


class StdioNativeTests(unittest.TestCase):
    # Reuse the existing finite filesystem/process fixture, not captured tests.
    setUp = CapturedNativeTests.setUp
    command = CapturedNativeTests.command
    await_file = CapturedNativeTests.await_file
    assert_retired = CapturedNativeTests.assert_retired
    assert_client_retired = CapturedNativeTests.assert_client_retired
    retain_cleanup = CapturedNativeTests.retain_cleanup
    kill_recorded_fixture = CapturedNativeTests.kill_recorded_fixture
    test_stdio_anchor_join_interruption_retries = (
        CapturedNativeTests.test_anchor_join_interruption_retries_before_confirming_retirement
    )

    def fact_command(self, *, budget, additions):
        command, environment = CapturedNativeTests.fact_command(self, budget=budget, additions=additions)
        self.assertEqual(1, command[2].count(" discard "))
        command[2] = command[2].replace(" discard ", " stdio ", 1)
        return command, environment

    def run_stdio(self, *, budget, additions):
        command, environment = self.fact_command(budget=budget, additions=additions)
        result = subprocess.run(command, env=environment, capture_output=True, timeout=5, check=False)
        self.retain_cleanup(result.stderr.decode(errors="replace"))
        return result, (self.root / "retired").read_text().strip()

    def test_publication_fault_is_owned_at_construction(self):
        with patch.dict(os.environ, {"NATIVE_PUBLICATION_FAILURE": "status"}):
            self.setUp()
        result, fact = self.run_stdio(budget="untimed", additions={})
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)
        self.assertEqual("true", fact)
        self.assert_client_retired()

    def test_missing_publication_fault_blocks_only_mv(self):
        del self.environment["NATIVE_PUBLICATION_FAILURE"]
        source, target = self.root / "publication.pending", self.root / "status"
        source.write_bytes(b"unpublished bytes\n")
        before = source.read_bytes(), source.stat().st_ino
        result = subprocess.run(
            [str(self.bin / "mv"), "-T", "--", str(source), str(target)],
            env=self.environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(1, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertIn(b"KeyError: 'NATIVE_PUBLICATION_FAILURE'", result.stderr)
        self.assertEqual(before, (source.read_bytes(), source.stat().st_ino))
        self.assertFalse(target.exists())
        self.assertFalse(self.control.exists())
        result = subprocess.run(
            [str(self.bin / "head"), "-c", "3", str(source)],
            env=self.environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual(before[0][:3], result.stdout)
        self.assertEqual(b"", result.stderr)
        result = subprocess.run(
            [str(self.bin / "rm"), "-f", "--", str(source)],
            env=self.environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)
        self.assertFalse(source.exists())
        self.assertFalse(target.exists())
        self.assertFalse(self.control.exists())

    def test_status_mutation_is_owned_at_construction(self):
        with patch.dict(os.environ, {"NATIVE_STATUS_MUTATION": "text"}):
            self.setUp()
        result, fact = self.run_stdio(budget="untimed", additions={})
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)
        self.assertEqual("true", fact)
        self.assert_client_retired()

    def test_missing_status_mutation_blocks_only_status_publish_and_read(self):
        del self.environment["NATIVE_STATUS_MUTATION"]
        source, target = self.root / "publication.pending", self.root / "status"
        source.write_bytes(b"unpublished bytes\n")
        before = source.read_bytes(), source.stat().st_ino
        read_status = self.root / "read/status"
        read_status.parent.mkdir()
        read_status.write_bytes(b"7\n")
        read_before = read_status.read_bytes(), read_status.stat().st_ino
        for command in (
            [str(self.bin / "mv"), "-T", "--", str(source), str(target)],
            [str(self.bin / "head"), "-c", "1", str(read_status)],
        ):
            with self.subTest(command=command[0]):
                result = subprocess.run(command, env=self.environment, capture_output=True, timeout=5, check=False)
                self.assertEqual(1, result.returncode, msg=result.stderr.decode())
                self.assertEqual(b"", result.stdout)
                self.assertIn(b"KeyError: 'NATIVE_STATUS_MUTATION'", result.stderr)
                self.assertEqual(before, (source.read_bytes(), source.stat().st_ino))
                self.assertEqual(read_before, (read_status.read_bytes(), read_status.stat().st_ino))
                self.assertFalse(target.exists())
                self.assertEqual({"anchor": 0, "directory": str(self.root)}, json.loads(self.control.read_text()))

        ordinary = self.root / "ordinary"
        result = subprocess.run(
            [str(self.bin / "mv"), "-T", "--", str(source), str(ordinary)],
            env=self.environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)
        self.assertFalse(source.exists())
        self.assertEqual(before, (ordinary.read_bytes(), ordinary.stat().st_ino))
        result = subprocess.run(
            [str(self.bin / "head"), "-c", "3", str(ordinary)],
            env=self.environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual(before[0][:3], result.stdout)
        self.assertEqual(b"", result.stderr)
        result = subprocess.run(
            [str(self.bin / "rm"), "-f", "--", str(ordinary)],
            env=self.environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)
        self.assertFalse(ordinary.exists())
        self.assertFalse(target.exists())
        self.assertEqual(read_before, (read_status.read_bytes(), read_status.stat().st_ino))
        self.assertEqual({"anchor": 0, "directory": str(self.root)}, json.loads(self.control.read_text()))

    def test_native_behavior_is_owned_and_missing_behavior_fails_before_observation(self):
        with patch.dict(os.environ, {"NATIVE_BEHAVIOR": "stream-finite"}):
            self.setUp()
        result, fact = self.run_stdio(budget="untimed", additions={})
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)
        self.assertEqual("true", fact)
        self.assert_client_retired()

        self.setUp()
        del self.environment["NATIVE_BEHAVIOR"]
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "native-client"],
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(1, result.returncode, msg=result.stderr.decode())
        self.assertEqual(b"", result.stdout)
        self.assertIn(b"KeyError: 'NATIVE_BEHAVIOR'", result.stderr)
        self.assertFalse(self.observation.exists())
        self.assertFalse(self.observation.with_suffix(".pending").exists())

    def retire_probe(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=3)

    def test_stdout_and_stderr_stream_binary_bytes_while_native_is_alive(self):
        command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "stream-live"})
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.addCleanup(self.retire_probe, process)
        for pipe, expected in ((process.stdout, b"stdout\0\r\n"), (process.stderr, b"stderr\0\r\n")):
            self.assertEqual([pipe], select.select([pipe], [], [], 2)[0])
            self.assertEqual(expected, os.read(pipe.fileno(), len(expected)))
        self.assertIsNone(process.poll())
        self.assertFalse((self.root / "retired").exists())
        observed = json.loads(self.observation.read_text())
        control = json.loads(self.control.read_text())
        scratch = Path(control["directory"])
        self.assertFalse((scratch / "output").exists())
        self.assertFalse((scratch / "command.log").exists())
        self.assertTrue(observed["stdin_eof"])
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=4)
        self.retain_cleanup(stderr.decode())
        self.assertEqual(143, process.returncode, msg=stderr.decode())
        self.assertEqual("true\n", (self.root / "retired").read_text())
        self.assert_client_retired()

    def test_stdout_and_stderr_over_eight_mib_are_not_capped(self):
        result, fact = self.run_stdio(budget="untimed", additions={"NATIVE_BEHAVIOR": "stream-large"})
        expected = bytes(range(256)) * 32769
        self.assertEqual(len(expected), len(result.stdout))
        self.assertEqual(len(expected), len(result.stderr))
        self.assertEqual(0, result.returncode, msg=result.stderr[:200].decode(errors="replace"))
        self.assertEqual(expected, result.stdout)
        self.assertEqual(expected, result.stderr)
        self.assertEqual("true", fact)
        self.assert_client_retired()

    def test_status_and_signal_dispositions_match_timed_and_untimed_calls(self):
        for budget in ("20", "untimed"):
            for status in (0, 7, 137):
                with self.subTest(budget=budget, status=status):
                    result, fact = self.run_stdio(
                        budget=budget, additions={"NATIVE_BEHAVIOR": "stream-finite", "NATIVE_STATUS": str(status)}
                    )
                    self.assertEqual(status, result.returncode, msg=result.stderr.decode())
                    self.assertEqual(b"stdout\0\r\n", result.stdout)
                    self.assertEqual(b"stderr\0\r\n", result.stderr)
                    self.assertNotIn(b"private capped output", result.stderr)
                    self.assertNotIn(b"command.log", result.stderr)
                    self.assertEqual("true", fact)
                    observed = json.loads(self.observation.read_text())
                    for key in ("sigint_ignored", "sigterm_ignored", "sighup_ignored"):
                        self.assertFalse(observed[key], msg=key)
                    self.assertTrue(observed["stdin_eof"])
                    self.assert_client_retired()

    def test_native_closed_outputs_do_not_establish_completion(self):
        command, environment = self.fact_command(
            budget="untimed", additions={"NATIVE_BEHAVIOR": "closed-output", "NATIVE_STATUS": "7"}
        )
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.addCleanup(self.retire_probe, process)
        self.await_file(self.root / "outputs-closed")
        observed = json.loads(self.observation.read_text())
        self.assertFalse(Path("/proc", str(observed["pid"]), "fd", "1").exists())
        self.assertFalse(Path("/proc", str(observed["pid"]), "fd", "2").exists())
        self.assertIsNone(process.poll())
        self.assertFalse((self.root / "retired").exists())
        _, stderr = process.communicate(timeout=4)
        self.assertEqual(7, process.returncode, msg=stderr.decode())
        self.assertEqual("true\n", (self.root / "retired").read_text())
        self.assert_client_retired()

    def test_held_public_pipe_descendant_is_retired_before_true_fact(self):
        result, fact = self.run_stdio(budget="untimed", additions={"NATIVE_BEHAVIOR": "held-descendant", "NATIVE_STATUS": "7"})
        self.assertEqual(7, result.returncode, msg=result.stderr.decode())
        self.assertEqual("true", fact)
        self.assert_client_retired()

    def test_blocked_public_output_cancellation_retires_every_owned_process(self):
        for stream in ("stdout", "stderr"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(stream=stream, signal=signum):
                    self.observation.unlink(missing_ok=True)
                    (self.root / "write-complete").unlink(missing_ok=True)
                    (self.root / "retired").unlink(missing_ok=True)
                    command, environment = self.fact_command(
                        budget="untimed",
                        additions={
                            "NATIVE_BEHAVIOR": "blocked-output",
                            "NATIVE_BLOCK_STREAM": stream,
                            "NATIVE_CLEANUP_FAILURE": "after-delete",
                        },
                    )
                    # The helper returns the diagnostic; publishing that result
                    # to this deliberately full pipe is the harness's decision.
                    diagnostic = self.root / "stdio-cleanup-diagnostic"
                    publication = 'printf "%s" "$inference_diagnostic" >&2'
                    self.assertEqual(1, command[2].count(publication))
                    command[2] = command[2].replace(
                        publication, 'printf "%s" "$inference_diagnostic" > "$NATIVE_STDIO_DIAGNOSTIC"', 1
                    )
                    environment["NATIVE_STDIO_DIAGNOSTIC"] = str(diagnostic)
                    process = subprocess.Popen(
                        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                    )
                    self.addCleanup(self.retire_probe, process)
                    self.await_file(self.observation)
                    observed = json.loads(self.observation.read_text())
                    wait_channel = Path("/proc", str(observed["pid"]), "wchan")
                    deadline = time.monotonic() + 2
                    while (
                        wait_channel.read_text().strip() not in {"pipe_write", "anon_pipe_write"} and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertIn(wait_channel.read_text().strip(), {"pipe_write", "anon_pipe_write"})
                    self.assertFalse((self.root / "write-complete").exists())
                    try:
                        process.send_signal(signum)
                        # Keep both consumers unread until owner exit. communicate
                        # would drain a full stderr pipe and conceal owner writes.
                        self.assertEqual(128 + signum, process.wait(timeout=4))
                        self.assertEqual("true\n", (self.root / "retired").read_text())
                        self.assert_client_retired()
                        self.assertFalse(Path(json.loads(self.control.read_text())["directory"]).exists())
                        scratch = json.loads(self.control.read_text())["directory"]
                        self.assertEqual(
                            f"Cannot confirm native client evidence directory removal; attempted path {scratch} (status 23).\n",
                            diagnostic.read_text(),
                        )
                    finally:
                        for identifier in (observed["pid"], observed["descendant"]):
                            self.kill_recorded_fixture(identifier)
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                        _, stderr = process.communicate(timeout=4)
                        self.retain_cleanup(stderr.decode())

    def test_first_stdio_child_sees_false_before_pid_capture_and_birth_cancellation(self):
        source = self.helper.read_text()
        launch = '        bash -c "$anchor_program" bash "$scratch/status" "$owning_parent" "$budget" "$timed_program" "$deadline_signal" "$grace" "$@" &\n'
        self.assertEqual(1, source.count(launch))
        sampled = (
            '        (printf "%s\\n" "$inference_retirement_confirmed" "$BASHPID" > "$NATIVE_BIRTH"; '
            'exec bash -c "$anchor_program" bash "$scratch/status" "$owning_parent" "$budget" "$timed_program" "$deadline_signal" "$grace" "$@") &\n'
        )
        source = source.replace(launch, sampled, 1)
        capture = "    leader=$!\n"
        self.assertEqual(1, source.count(capture))
        self.helper.write_text(source.replace(capture, "    sleep 0.25\n" + capture, 1))
        receipt = self.root / "birth"
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                receipt.unlink(missing_ok=True)
                command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BIRTH": str(receipt)})
                process = subprocess.Popen(
                    command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(receipt)
                fact, anchor = receipt.read_text().splitlines()
                self.assertEqual("false", fact)
                process.send_signal(signum)
                _, stderr = process.communicate(timeout=4)
                self.assertEqual(128 + signum, process.returncode, msg=stderr.decode())
                self.assertEqual("true\n", (self.root / "retired").read_text())
                self.assert_retired(int(anchor))
                self.assertFalse(self.observation.exists())

    def test_stdio_cleanup_failure_returns_attempt_truth_without_changing_native_streams(self):
        for effect in ("1", "after-delete"):
            for native_status, expected in (("0", 23), ("7", 7)):
                with self.subTest(effect=effect, status=native_status):
                    diagnostic = self.root / "stdio-removal-diagnostic"
                    outcome = self.root / "stdio-removal-outcome"
                    script = (
                        'source "$1"; inference_diagnostic=; '
                        "trap 'status=$?; trap - EXIT; "
                        'printf "%s %s %s\\n" "$status" "$inference_cancellation_status" "$inference_retirement_confirmed" > "$NATIVE_STDIO_OUTCOME"; '
                        'printf "%s" "$inference_diagnostic" > "$NATIVE_STDIO_DIAGNOSTIC"; exit "$status"\' EXIT; '
                        'inference_native probe untimed stdio "$2" "$3" native-client'
                    )
                    result = subprocess.run(
                        ["/bin/bash", "-euc", script, "bash", str(self.helper), sys.executable, str(Path(__file__).resolve())],
                        env=self.environment
                        | {
                            "NATIVE_STATUS": native_status,
                            "NATIVE_BEHAVIOR": "stream-finite",
                            "NATIVE_CLEANUP_FAILURE": effect,
                            "NATIVE_STDIO_OUTCOME": str(outcome),
                            "NATIVE_STDIO_DIAGNOSTIC": str(diagnostic),
                        },
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(expected, result.returncode, msg=result.stderr)
                    self.assertEqual(b"stdout\0\r\n", result.stdout)
                    self.assertEqual(b"stderr\0\r\n", result.stderr)
                    self.assertEqual(f"{expected} 0 true\n", outcome.read_text())
                    scratch = Path(json.loads(self.control.read_text())["directory"])
                    payload = diagnostic.read_text()
                    self.retain_cleanup(payload)
                    self.assertEqual(
                        f"Cannot confirm native client evidence directory removal; attempted path {scratch} (status 23).\n",
                        payload,
                    )
                    self.assertNotIn("retained", payload)
                    self.assertEqual(effect == "1", scratch.exists())
                    self.assert_client_retired()

    def test_receipt_and_startup_faults_do_not_invent_capture_evidence(self):
        for additions, expected, fact in (
            ({"NATIVE_PUBLICATION_FAILURE": "ready"}, 1, "false"),
            ({"NATIVE_PUBLICATION_FAILURE": "release"}, 29, "true"),
            ({"NATIVE_PUBLICATION_FAILURE": "status", "NATIVE_BEHAVIOR": "held-descendant"}, 1, "true"),
            ({"NATIVE_STATUS_MUTATION": "fifo", "NATIVE_BEHAVIOR": "held-descendant"}, 1, "true"),
            ({"NATIVE_STATUS_MUTATION": "read-error", "NATIVE_BEHAVIOR": "held-descendant"}, 1, "true"),
            ({"NATIVE_CLEANUP_FAILURE": "1"}, 23, "true"),
            ({"NATIVE_CLEANUP_FAILURE": "1", "NATIVE_STATUS": "7"}, 7, "true"),
            ({"NATIVE_CLEANUP_FAILURE": "after-delete"}, 23, "true"),
            ({"NATIVE_CLEANUP_FAILURE": "after-delete", "NATIVE_STATUS": "7"}, 7, "true"),
        ):
            with self.subTest(additions=additions):
                result, observed_fact = self.run_stdio(budget="untimed", additions=additions)
                self.assertEqual(expected, result.returncode, msg=result.stderr.decode())
                self.assertEqual(fact, observed_fact)
                self.assertNotIn(b"command.log", result.stderr)
                self.assertNotIn(b"private capped output", result.stderr)
                if additions.get("NATIVE_CLEANUP_FAILURE") == "after-delete":
                    self.assertFalse(Path(json.loads(self.control.read_text())["directory"]).exists())
                    scratch = json.loads(self.control.read_text())["directory"]
                    self.assertEqual(
                        f"Cannot confirm native client evidence directory removal; attempted path {scratch} (status 23).\n".encode(),
                        result.stderr,
                    )
                self.assert_retired(json.loads(self.control.read_text())["anchor"])
                if self.observation.exists():
                    self.assert_client_retired()

    def test_fact_is_false_before_anchor_join(self):
        source = self.helper.read_text()
        join = '            wait "$leader" 2>/dev/null || :\n'
        self.assertEqual(1, source.count(join))
        receipt = self.root / "before-join"
        self.helper.write_text(
            source.replace(join, '            printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_JOIN_FACT"\n' + join, 1)
        )
        result, fact = self.run_stdio(budget="untimed", additions={"NATIVE_JOIN_FACT": str(receipt)})
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual("false\n", receipt.read_text())
        self.assertEqual("true", fact)

    def test_unknown_retirement_exit_is_not_public_pipe_eof(self):
        command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "stream-live"})
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.addCleanup(self.retire_probe, process)
        self.await_file(self.observation)
        observed = json.loads(self.observation.read_text())
        os.kill(observed["parent"], signal.SIGKILL)
        try:
            self.assertEqual(1, process.wait(timeout=4))
            self.assertEqual("false\n", (self.root / "retired").read_text())
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                while True:
                    try:
                        chunk = os.read(pipe.fileno(), 4096)
                    except BlockingIOError:
                        break
                    self.assertNotEqual(b"", chunk, msg="unconfirmed surviving writer unexpectedly reached EOF")
            self.assertTrue(Path("/proc", str(observed["descendant"])).exists())
        finally:
            self.kill_recorded_fixture(observed["pid"])
            self.kill_recorded_fixture(observed["descendant"])
        for pipe in (process.stdout, process.stderr):
            os.set_blocking(pipe.fileno(), True)
        _, stderr = process.communicate(timeout=4)
        self.retain_cleanup(stderr.decode())
        self.assertNotIn(b"command.log", stderr)


def fixture_command(name):
    """Inject only named local file faults; all other operations use native tools."""
    arguments = sys.argv[1:]
    if name == "mv":
        phase = os.environ["NATIVE_PUBLICATION_FAILURE"]
        source, target = map(Path, arguments[-2:])
        receipt = target.parent / "status.ready"
        if target.name == "status.ready":
            anchor = int(source.read_text())
        elif receipt.exists():
            anchor = int(receipt.read_text())
        else:
            anchor = 0
        Path(os.environ["NATIVE_CONTROL"]).write_text(json.dumps({"anchor": anchor, "directory": str(target.parent)}))
        if target.name == "status.ready" and os.environ.get("NATIVE_CORRUPT_READY") == "1":
            source.write_bytes(b"not-the-anchor\n")
        if target.name == "status.release" and phase.startswith("release-"):
            if phase == "release-fifo":
                os.mkfifo(target)
            elif phase == "release-directory":
                target.mkdir()
            else:
                target.symlink_to(source)
            return 0
        if target.name == "status." + phase or (target.name == "status" and phase == "status"):
            if os.environ.get("NATIVE_CANCEL_PUBLICATION") == "1":
                parent = int(Path("/proc", str(anchor), "stat").read_text().split()[3])
                os.kill(parent, signal.SIGTERM)
            return 29
        if target.name == "status":
            mutation = os.environ["NATIVE_STATUS_MUTATION"]
            values = {
                "nul": b"0\0\n",
                "cr": b"0\r\n",
                "missing-lf": b"0",
                "extra-lf": b"0\n\n",
                "extra-line": b"0\n7\n",
                "negative": b"-1\n",
                "256": b"256\n",
                "text": b"unknown\n",
            }
            if mutation in values:
                source.write_bytes(values[mutation])
            if mutation == "symlink":
                target.symlink_to(source)
                return 0
            if mutation in {"fifo", "directory", "deadline-fifo", "deadline-directory", "deadline-symlink"}:
                if mutation.startswith("deadline-"):
                    target = target.parent / "status.deadline"
                if mutation.endswith("fifo"):
                    os.mkfifo(target)
                elif mutation.endswith("directory"):
                    target.mkdir()
                else:
                    target.symlink_to(source)
                return 0
    if name == "head" and arguments[-1].endswith("/status") and os.environ["NATIVE_STATUS_MUTATION"] == "read-error":
        return 31
    if name == "head" and arguments == ["-c", "8388608"] and os.environ.get("NATIVE_DRAIN_FAILURE") == "1":
        return 33
    if name == "rm" and os.environ.get("NATIVE_CLEANUP_FAILURE") == "1":
        if "NATIVE_CANCEL_CLEANUP" in os.environ:
            os.kill(os.getppid(), int(os.environ["NATIVE_CANCEL_CLEANUP"]))
        return 23
    if name == "rm" and os.environ.get("NATIVE_CLEANUP_FAILURE") == "after-delete":
        # Actual deletion plus a secondary failure must not invent retained files
        # or block the stdio owner by writing another message to its native pipe.
        subprocess.run(["/bin/rm", *arguments], check=True)
        if "NATIVE_CANCEL_CLEANUP" in os.environ:
            os.kill(os.getppid(), int(os.environ["NATIVE_CANCEL_CLEANUP"]))
        sys.stderr.write("fixture cleanup failed after deletion\n")
        return 23
    os.execv("/bin/" + name, [name, *arguments])
    return 97


def native_client():
    """Model a finite native status, resistant group member or output-only EOF."""
    observation = Path(os.environ["NATIVE_OBSERVATION"])
    behavior = os.environ["NATIVE_BEHAVIOR"]
    observed = {
        "pid": os.getpid(),
        "parent": os.getppid(),
        "sigint_ignored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN,
        "sigterm_ignored": signal.getsignal(signal.SIGTERM) == signal.SIG_IGN,
        "sighup_ignored": signal.getsignal(signal.SIGHUP) == signal.SIG_IGN,
        "stdin_eof": os.read(0, 1) == b"",
    }
    if behavior in {"resistant", "held-descendant", "stream-live", "blocked-output", "original-signals"}:
        descendant = subprocess.Popen(["/bin/bash", "-c", "trap '' HUP INT TERM; exec sleep 60"])
        observed["descendant"] = descendant.pid
    observation.with_suffix(".pending").write_text(json.dumps(observed))
    observation.with_suffix(".pending").replace(observation)
    if behavior in {"stream-live", "stream-finite"}:
        os.write(1, b"stdout\0\r\n")
        os.write(2, b"stderr\0\r\n")
    if behavior == "stream-live":
        time.sleep(60)
    if behavior == "stream-large":
        data = bytes(range(256)) * 32769
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()
    if behavior == "blocked-output":
        stream = os.environ["NATIVE_BLOCK_STREAM"]
        if stream == "stdout":
            sys.stdout.buffer.write(b"x" * 8388609)
            sys.stdout.buffer.flush()
        elif stream == "stderr":
            sys.stderr.buffer.write(b"x" * 8388609)
            sys.stderr.buffer.flush()
        else:
            raise ValueError("Blocked-output fixture requires explicit stdout or stderr")
        (observation.parent / "write-complete").write_text("finished")
    if behavior == "original-signals":

        def record_signal(signum, _frame):
            with (observation.parent / "signal-observed").open("a") as marker:
                marker.write(signal.Signals(signum).name.removeprefix("SIG") + "\n")

        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, record_signal)
        ignored = sum(1 << (signum - 1) for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM))
        descendant_status = Path("/proc", str(descendant.pid), "status")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            fields = dict(line.split(":", 1) for line in descendant_status.read_text().splitlines() if ":" in line)
            if int(fields["SigIgn"].strip(), 16) & ignored == ignored:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("The actual resistant descendant did not ignore HUP, INT and TERM before readiness.")
        (observation.parent / "container-handlers-ready").write_text("ready\n")
        time.sleep(60)
    if behavior == "resistant":
        signal.signal(signal.SIGTERM, lambda *_: (observation.parent / "term-observed").write_text("TERM\n"))
        time.sleep(60)
    if behavior == "late-success":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(0.3)
    if behavior == "slow-success":
        time.sleep(0.4)
    if behavior == "closed-output":
        os.close(1)
        os.close(2)
        (observation.parent / "outputs-closed").write_text("closed")
        time.sleep(0.25)
    return int(os.environ["NATIVE_STATUS"])


if __name__ == "__main__":
    command_name = Path(sys.argv[0]).name
    if command_name in {"mv", "head", "rm"}:
        raise SystemExit(fixture_command(command_name))
    if sys.argv[1:] == ["native-client"]:
        raise SystemExit(native_client())
    unittest.main()
