"""Fresh-operation fault contracts through real subprocesses and /proc.

The Docker executable is a persistent engine model, not a real Docker daemon.
The host-namespace precondition and Unix-socket stat are substituted (this
sandbox forbids socket creation). Process identity, command supervision, observation
and retirement are the production code.
"""
import json
import os
from pathlib import Path
import signal
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from linux_idmap._files import FileIdentity
from linux_idmap import IdMap
from linux_idmap._procfs import ReaderEvidence
from linux_idmap.core import parse_map as parse_id_map
from linux_idmap._errors import IdentityError
from linux_idmap._probe_process import ProbeProcesses
from linux_idmap._probe_model import Bounds, ProbeFailure, ProbeRequest, ResourceState
from linux_idmap.probe import probe_identity

class OperationFaultTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_path = self.root / "engine.json"
        self.state_path.write_text(json.dumps({"calls": [], "container": None}))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fixture = Path(__file__).parent / "fixtures" / "fake_docker.py"
        launcher = self.bin / "docker"
        # Exercise this matrix interpreter, without ambient site/.pth startup.
        launcher.write_text(f"#!/usr/bin/env -S {shlex.quote(sys.executable)} -I -S -B\n"
                            + fixture.read_text().split("\n", 1)[1])
        launcher.chmod(0o755)
        (self.root / "engine.sock").touch()
        def fixture_socket_identity(path):
            metadata = Path(path).stat()
            return FileIdentity.of(metadata)
        self.enterContext(patch("linux_idmap._docker._socket_identity", side_effect=fixture_socket_identity))
        self.enterContext(patch.dict(os.environ, {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "LINUX_IDMAP_TEST_STATE": str(self.state_path),
        }))
        self.enterContext(patch.object(ProbeProcesses, "reader_evidence", new=self.reader_evidence))
        self.addCleanup(self.kill_fixture_processes)
        # Leave acquisition scheduling room; guarded commands launch several clients.
        # Command/cleanup/lifetime use production defaults. Tight timeout behavior
        # is tested separately in test_docker.py.
        self.request = ProbeRequest(
            endpoint="unix://" + str(self.root / "engine.sock"), image="approved:local",
            interpreter="/opt/image/bin/python", user_uid=1000, user_gid=2000,
            bounds=Bounds(operation_seconds=60),
        )

    @staticmethod
    def reader_evidence(_guard=None):
        proc = Path("/proc").stat()
        return ReaderEvidence(4026531837, FileIdentity.of(proc),
                            os.getpid(), threading.get_native_id())

    def state(self):
        return json.loads(self.state_path.read_text())

    def configure(self, **values):
        state = self.state()
        state.update(values)
        self.state_path.write_text(json.dumps(state))

    def calls(self, kind):
        return [call for call in self.state()["calls"] if call["kind"] == kind]

    def assert_fault_reached(self, kind):
        self.assertEqual(kind, self.state().get("fault_reached"), f"{kind} fault was not reached")
        self.assertEqual(1, len(self.calls(kind)))

    def kill_fixture_processes(self):
        # Only PIDs created by this test fixture are considered; command clients
        # are already reaped by the production transport.
        # silent-defaults: allow-contract -- The fixture records client PIDs only after a command starts; no recorded clients means nothing to reap.
        for pid in self.state().get("client_pids", []):
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if str(self.bin / "docker").encode() in argv:
                    os.kill(pid, signal.SIGKILL)
            # silent-defaults: allow-contract -- A vanished fixture client needs no signal; other cleanup errors remain visible.
            except (FileNotFoundError, ProcessLookupError):
                pass
        # silent-defaults: allow-contract -- The fixture records probe processes only after start; absence means cleanup has no modeled process to kill.
        for process in self.state().get("probe_processes", []):
            directory = Path(f"/proc/{process['pid']}")
            try:
                argv = (directory / "cmdline").read_bytes().split(b"\0")
                if process["token"].encode() in argv:
                    os.kill(process["pid"], signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError):
                continue
            # The fake daemon's children are adopted by the test environment's
            # init after the CLI exits. Verify they are reaped, not just killed.
            deadline = time.monotonic() + 3
            while directory.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(directory.exists(), msg="fixture probe was not reaped")

    def failure(self, cancel=None):
        with self.assertRaises(ProbeFailure) as caught:
            probe_identity(self.request, cancel=cancel)
        report = caught.exception.as_dict()
        self.assertNotIn("uid_map", report)
        self.assertNotIn("gid_map", report)
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertTrue(caught.exception.problem.message)
        if caught.exception.retirement.state != ResourceState.NOT_CREATED:
            self.assertIsNotNone(caught.exception.handle)
            self.assertEqual(self.request.endpoint, caught.exception.handle.endpoint)
            self.assertRegex(caught.exception.handle.operation_token, r"^[0-9a-f]{32}$")
        return caught.exception

    def test_success_uses_selected_endpoint_and_retires_real_probe_process(self):
        result = probe_identity(self.request)
        # The fixture explicitly substitutes qualification for both observation and reference.
        expected = IdMap(list(parse_id_map(Path("/proc/self/uid_map").read_bytes()))).translate(os.getuid())
        self.assertEqual(expected, result.uid_map.translate(os.getuid()))
        self.assertIsNone(self.state()["container"])
        self.assertEqual(1, len(self.calls("create")))
        self.assertEqual(1, len(self.calls("rm")))
        self.assertTrue(all(call["endpoint"] == self.request.endpoint
                            for call in self.state()["calls"]))
        self.assertFalse(self.calls("context"))

    def test_lost_create_reply_reconciles_without_second_creation(self):
        self.configure(hang_after="create")
        failure = self.failure()
        self.assert_fault_reached("create")
        self.assertEqual("timeout", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual(1, len(self.calls("rm")))
        self.assertIsNone(self.state()["container"])

    def test_lost_start_reply_still_retires_live_probe(self):
        self.configure(hang_after="start")
        failure = self.failure()
        self.assert_fault_reached("start")
        self.assertEqual("timeout", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual(1, len(self.calls("rm")))
        self.assertIsNone(self.state()["container"])

    def test_lost_remove_reply_can_confirm_absence_without_repeating_remove(self):
        self.configure(hang_after="rm")
        try:
            result = probe_identity(self.request)
        finally:
            self.assert_fault_reached("rm")
        self.assertEqual("c" * 64, result.container_id)
        self.assertIsNone(self.state()["container"])

    def test_hidden_creation_stays_uncertain_without_duplicate_creation(self):
        self.configure(hang_after="create", hide_container=True)
        failure = self.failure()
        self.assert_fault_reached("create")
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertFalse(self.calls("rm"))
        self.assertIsNotNone(self.state()["container"])
        self.assertIsNone(failure.handle.container_id)

    def test_foreign_token_is_never_removed(self):
        self.configure(foreign_token=True)
        failure = self.failure()
        self.assertEqual("ownership", failure.problem.category)
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertFalse(self.calls("rm"))
        self.assertIsNotNone(self.state()["container"])

    def test_unreadable_maps_do_not_prevent_removal_of_verified_owned_probe(self):
        with patch.object(ProbeProcesses, "observe", side_effect=IdentityError("process-identity", "probe maps unavailable")) as observe:
            failure = self.failure()
        observe.assert_called_once()
        self.assertEqual(1, len(self.calls("rm")))
        self.assertEqual("process-identity", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertIsNone(self.state()["container"])


    def test_container_absence_without_process_retirement_never_releases_maps(self):
        self.configure(leave_process_alive=True)
        original = ProbeProcesses.process_retired
        checked_live_process = False
        def observe_retirement(guard, identity):
            nonlocal checked_live_process
            retired = original(guard, identity)
            if not retired:
                checked_live_process = True
            return retired
        with patch.object(ProbeProcesses, "process_retired", new=observe_retirement):
            failure = self.failure()
        self.assertTrue(checked_live_process, "real process retirement check was not reached")
        self.assertEqual(1, len(self.calls("rm")))
        self.assertIsNone(self.state()["container"])
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertTrue(self.state()["probe_processes"])

    def test_changed_engine_never_authorizes_removal_on_replacement(self):
        self.configure(change_engine_after_create=True)
        failure = self.failure()
        self.assertEqual(ResourceState.UNRESOLVED, failure.retirement.state)
        self.assertEqual(1, len(self.calls("create")))
        self.assertFalse(self.calls("start"))
        self.assertFalse(self.calls("rm"))
        self.assertIsNotNone(self.state()["container"])
        self.assertEqual("fixture-engine", failure.handle.daemon_id)

    def test_cancellation_after_creation_still_retires_owned_resource(self):
        self.configure(hang_after="create")
        cancellation = threading.Event()
        stop = threading.Event()
        reached_creation = threading.Event()
        def cancel_created():
            while not stop.is_set():
                if self.state().get("container") is not None:
                    reached_creation.set()
                    cancellation.set()
                    return
                stop.wait(0.01)
        watcher = threading.Thread(target=cancel_created)
        watcher.start()
        try:
            failure = self.failure(cancel=cancellation)
        finally:
            stop.set()
            watcher.join(timeout=2)
        self.assertFalse(watcher.is_alive())
        self.assertTrue(reached_creation.is_set())
        self.assert_fault_reached("create")
        self.assertEqual("cancelled", failure.problem.category)
        self.assertEqual(ResourceState.RETIRED, failure.retirement.state)
        self.assertEqual(1, len(self.calls("rm")))
        self.assertIsNone(self.state()["container"])


if __name__ == "__main__":
    unittest.main()
