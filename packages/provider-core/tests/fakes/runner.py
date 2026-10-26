"""Real child processes exercise lifecycle without importing scientific libraries."""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from provider_core.runner_session import RunnerSession


WORKER = """
import argparse, json, os, signal, socket, stat, sys, time
parser = argparse.ArgumentParser()
parser.add_argument('--session-fd', type=int)
parser.add_argument('--lease-fd', type=int)
parser.add_argument('--operation')
args = parser.parse_args()
with socket.socket(fileno=args.session_fd) as channel:
    command = channel.recv(1)
    if command:
        print('event=fixture_scientific_input operation=' + args.operation, file=sys.stderr, flush=True)
    if command == b'H':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if command == b'C':
        os.chmod('.', 0)
    channel.sendall(str(os.getpid()).encode() + b'\\n')
    if command == b'R':
        while channel.recv(1):
            channel.sendall(str(os.getpid()).encode() + b'\\n')
    if command == b'D':
        sockets = []
        for leaf in os.listdir('/proc/self/fd'):
            try:
                if stat.S_ISSOCK(os.fstat(int(leaf)).st_mode): sockets.append(int(leaf))
            except OSError: pass
        lease = os.fstat(args.lease_fd)
        channel.sendall(json.dumps({'sockets': sockets, 'science': args.session_fd,
                                   'lock': [lease.st_dev, lease.st_ino]}).encode() + b'\\n')
    if command == b'H':
        while True: time.sleep(1)
    if command == b'F':
        channel.sendall(b'\\x00\\x00')
        raise SystemExit(9)
"""


class RunningSupervisor:
    """Own one disposable supervisor and its private filesystem grant."""

    # silent-defaults: allow-contract -- The ordinary disposable supervisor is bounded; explicit None selects control-owner lifetime.
    def __init__(self, *, operation_seconds=5, role="fixture", worker_argv=None):
        self.role = role
        self._closed = False
        self.temporary = tempfile.TemporaryDirectory(prefix="runner-")
        self.root = Path(self.temporary.name)
        self.endpoint = self.root / "endpoint"
        self.scratch = self.root / "scratch"
        self.endpoint.mkdir(mode=0o700)
        self.scratch.mkdir(mode=0o700)
        self.status = self.root / "status"
        self.status.mkdir(mode=0o700)
        (self.endpoint / ".owner-session.lock").touch(mode=0o600)
        self.path = self.endpoint / "runner.sock"
        # silent-defaults: allow-contract -- This fixture's explicit None selects owner-session lifetime; numeric budgets select the finite CLI.
        lifetime = ["--owner-session-lifetime"] if operation_seconds is None else ["--operation-seconds", str(operation_seconds)]
        # silent-defaults: allow-contract -- Absent synthetic worker argv selects this fixture worker; nonempty argv injects an explicit fault.
        self.command = [sys.executable, "-m", "provider_core.runner", "--role", self.role, "--socket", str(self.path),
             "--scratch", str(self.scratch), "--status-directory", str(self.status), *lifetime,
             "--startup-seconds", "1", "--terminate-seconds", "0.1", "--kill-seconds", "1", "--cleanup-seconds", "1",
             "--", *(worker_argv or [sys.executable, "-c", WORKER])]
        self._start()

    def _start(self):
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while not self.path.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.close()
                raise RuntimeError("Disposable supervisor did not open its endpoint")
            time.sleep(0.01)

    def restart_with_fresh_scratch(self):
        """Fixture supplies a new scratch grant; this does not prove Docker teardown."""
        self.process.communicate(timeout=2)
        self.path.unlink()
        self.scratch = self.root / "successor-scratch"
        self.scratch.mkdir(mode=0o700)
        self.command[self.command.index("--scratch") + 1] = str(self.scratch)
        self._start()

    def session(self, **budgets):
        observed = (self.endpoint / ".owner-session.lock").stat()
        return RunnerSession(self.path, role=self.role, expected_lock_identity=(observed.st_dev, observed.st_ino), **budgets)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None:
            self.process.terminate()
        try:
            _, self.diagnostic = self.process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _, self.diagnostic = self.process.communicate(timeout=3)
            raise
        finally:
            self.temporary.cleanup()


# silent-defaults: allow-contract -- The fixture uses a fixed control command unless a scenario selects another.
def worker_pid(channel, command=b"S"):
    channel.settimeout(2)
    channel.sendall(command)
    reply = bytearray()
    while not reply.endswith(b"\n"):
        part = channel.recv(1)
        if not part:
            raise RuntimeError("Scientific child closed before reporting its process identity")
        reply.extend(part)
    return int(reply)
