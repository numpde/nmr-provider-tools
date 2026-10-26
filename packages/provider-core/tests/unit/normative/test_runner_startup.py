"""A requested stop cannot open a new runner control operation."""

import array
from concurrent.futures import ThreadPoolExecutor
import errno
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import provider_core.runner_session as runner_api
from provider_core.runner_protocol import send_document, send_greeting
from provider_core.runner_session import RunnerSession


class RunnerStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "runner.sock"
        self.listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.listener.close)
        self.listener.bind(str(self.path))
        self.listener.listen()
        self.listener.settimeout(1)
        self.greeting = {
            "kind": "opened", "role": "fixture", "generation": "a" * 32,
            "operation": "b" * 32, "lock": [0, 0],
        }

    def session(self, control_type):
        with patch.object(socket, "socket", control_type):
            session = RunnerSession(
                self.path, role="fixture", expected_lock_identity=(0, 0), startup_seconds=3, retirement_seconds=1,
            )
        self.addCleanup(session.close)
        return session

    def test_stop_before_connect_does_not_reach_the_listening_peer(self):
        session = self.session(socket.socket)
        self.assertFalse(session.control_connected)
        session.request_stop()
        session.request_stop()
        with self.assertRaises(InterruptedError) as stopped:
            session.connect()
        self.assertIs(runner_api.RunnerStartupStopped, type(stopped.exception))
        self.assertFalse(session.control_connected)
        self.assertIsNone(session.science)
        self.assertIsNone(session.operation)
        self.assertIsNone(session.retirement)
        self.listener.setblocking(False)
        with self.assertRaises(BlockingIOError):
            self.listener.accept()

    def test_stop_interrupts_missing_endpoint_retries(self):
        self.listener.close()
        self.path.unlink()
        self.assert_retry_stops(errno.ENOENT)

    def test_stop_interrupts_refused_endpoint_retries(self):
        self.listener.close()
        self.assert_retry_stops(errno.ECONNREFUSED)

    def assert_retry_stops(self, expected_errno):
        attempted = threading.Event()
        failures = []

        class ObservedConnect(socket.socket):
            def connect(self, address):
                try:
                    return super().connect(address)
                except OSError as cause:
                    failures.append(cause.errno)
                    attempted.set()
                    raise

        session = self.session(ObservedConnect)
        with ThreadPoolExecutor(max_workers=1) as executor:
            original = executor.submit(session.connect)
            self.assertTrue(attempted.wait(1))
            self.assertEqual(expected_errno, failures[0])
            session.request_stop()
            with self.assertRaises(InterruptedError) as stopped:
                original.result(timeout=1)
        self.assertIs(runner_api.RunnerStartupStopped, type(stopped.exception))
        self.assertFalse(session.control_connected)
        self.assertIsNone(session.science)
        self.assertIsNone(session.retirement)

    def test_foreign_connect_interruption_keeps_its_original_identity(self):
        failure = InterruptedError(errno.EINTR, "Fixture interrupted the transport connect.")

        class InterruptedConnect(socket.socket):
            def connect(self, _address):
                raise failure

        session = self.session(InterruptedConnect)
        with self.assertRaises(InterruptedError) as caught:
            session.connect()
        self.assertIs(failure, caught.exception)
        self.assertIsNot(runner_api.RunnerStartupStopped, type(caught.exception))
        self.assertFalse(session.control_connected)
        self.assertIsNone(session.retirement)

    def test_stop_racing_successful_kernel_connect_half_closes_the_accepted_peer(self):
        connected, release = threading.Event(), threading.Event()

        class PausedConnect(socket.socket):
            def connect(self, address):
                super().connect(address)
                connected.set()
                if not release.wait(1):
                    raise TimeoutError("Fixture did not release the successful control connection.")

        session = self.session(PausedConnect)
        with ThreadPoolExecutor(max_workers=1) as executor:
            original = executor.submit(session.connect)
            try:
                self.assertTrue(connected.wait(1))
                session.request_stop()
            finally:
                release.set()
            peer, _ = self.listener.accept()
            science, remote = socket.socketpair()
            with peer, science, remote:
                peer.settimeout(1)
                self.assertEqual(b"", peer.recv(1))
                send_greeting(peer, self.greeting, science, deadline=time.monotonic() + 1)
                original.result(timeout=1)
                self.assertTrue(session.control_connected)
                send_document(peer, {
                    "kind": "retired", "generation": self.greeting["generation"],
                    "operation": self.greeting["operation"], "returncode": -15, "scratch_released": True,
                }, deadline=time.monotonic() + 1)
                session.retire()
        self.assertEqual(-15, session.retirement.returncode)
        self.assertTrue(session.retirement.scratch_released)

    def test_stop_preserves_the_original_partial_greeting_and_transferred_science_endpoint(self):
        session = self.session(socket.socket)
        raw = json.dumps(self.greeting).encode("ascii")
        frame = struct.pack("!I", len(raw)) + raw
        with ThreadPoolExecutor(max_workers=1) as executor:
            original = executor.submit(session.connect)
            peer, _ = self.listener.accept()
            science, remote = socket.socketpair()
            with peer, science, remote:
                peer.settimeout(1)
                peer.sendmsg([b"\0" + frame[:2]], [
                    (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [science.fileno()])),
                ])
                science.close()
                session.request_stop()
                self.assertEqual(b"", peer.recv(1))
                remote.setblocking(False)
                with self.assertRaises(BlockingIOError):
                    remote.recv(1)
                peer.sendall(frame[2:])
                original.result(timeout=1)
                self.assertEqual(self.greeting["operation"], session.operation)
                send_document(peer, {
                    "kind": "retired", "generation": session.generation, "operation": session.operation,
                    "returncode": 0, "scratch_released": True,
                }, deadline=time.monotonic() + 1)
                session.retire()
                self.assertEqual(0, session.retirement.returncode)
                self.assertTrue(session.retirement.scratch_released)
                session.close()
                self.assertTrue(session.control_connected)
                remote.settimeout(1)
                self.assertEqual(b"", remote.recv(1))
