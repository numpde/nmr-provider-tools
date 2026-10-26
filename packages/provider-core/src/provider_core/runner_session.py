"""Retain one control session separately from its scientific endpoint."""

from provider_core.container_guard import require_container

require_container()

import socket
import time
import errno
from dataclasses import dataclass

from provider_core.runner_protocol import LifecycleRejected, positive_seconds, receive_document, receive_greeting


@dataclass(frozen=True, slots=True)
class Retirement:
    returncode: int
    scratch_released: bool


class RunnerSession:
    """A synchronous I/O owner, retained by async callers through their original task.

    Construct before starting connect. request_stop half-closes control immediately;
    it does not wait for scientific exchange or claim that cleanup has completed.
    """

    def __init__(self, path, *, role, expected_lock_identity, startup_seconds, retirement_seconds):
        positive_seconds(startup_seconds)
        positive_seconds(retirement_seconds)
        self.path, self._startup, self._cleanup = str(path), startup_seconds, retirement_seconds
        self.role, self.expected_lock_identity = role, tuple(expected_lock_identity)
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.science = None
        self.generation = self.operation = self.lock_identity = None
        self.retirement = None
        self._stop_requested = False
        self._observation_failure = None
        self._connect_started = False

    def connect(self, *, deadline=None):
        if self._connect_started:
            raise ValueError("Runner session connect is single-use; retain its original setup operation.")
        self._connect_started = True
        phase_deadline = time.monotonic() + self._startup
        deadline = phase_deadline if deadline is None else min(deadline, phase_deadline)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Runner did not become ready within its startup budget.")
            self.control.settimeout(remaining)
            try:
                self.control.connect(self.path)
                break
            except OSError as cause:
                if cause.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        greeting, self.science = receive_greeting(self.control, deadline=deadline)
        if greeting["role"] != self.role or tuple(greeting["lock"]) != self.expected_lock_identity:
            self.science.close()
            self.science = None
            raise LifecycleRejected("Runner greeting does not match the admitted role and stable lifetime lock.")
        self.generation, self.operation = greeting["generation"], greeting["operation"]
        self.lock_identity = tuple(greeting["lock"])

    def request_stop(self):
        if not self._stop_requested:
            self.control.shutdown(socket.SHUT_WR)
            self._stop_requested = True

    def retire(self, *, deadline=None):
        if self.retirement is None:
            self.request_stop()
        self.observe_retirement(deadline=deadline)

    def observe_retirement(self, *, deadline=None):
        """Receive cleanup facts without causing retirement (including cap expiry)."""
        if self._observation_failure is not None:
            raise LifecycleRejected(
                "Original runner retirement read failed; preserve its cause and use bounded lifecycle recovery, not a second frame read."
            ) from self._observation_failure
        if self.retirement is None:
            self.control.settimeout(self._cleanup)
            try:
                phase_deadline = time.monotonic() + self._cleanup
                effective = phase_deadline if deadline is None else min(deadline, phase_deadline)
                self.retirement = self._receive_retirement(effective)
            except Exception as cause:
                # A partial frame cannot restart at its remaining bytes. Retain
                # this owner until the binding's proof session settles it.
                self._observation_failure = cause
                raise
        if not self.retirement.scratch_released:
            raise LifecycleRejected("Runner child stopped but its scratch cleanup remains unconfirmed.",
                                    returncode=self.retirement.returncode, scratch_released=False)

    def _receive_retirement(self, deadline):
        reply = receive_document(self.control, deadline=deadline)
        if (
            set(reply) != {"kind", "generation", "operation", "returncode", "scratch_released"}
            or reply["kind"] != "retired" or reply["generation"] != self.generation
            or reply["operation"] != self.operation or type(reply["returncode"]) is not int
            or type(reply["scratch_released"]) is not bool
        ):
            raise LifecycleRejected("Runner retirement is malformed or belongs to another operation.")
        return Retirement(reply["returncode"], reply["scratch_released"])

    def close(self):
        if self.science is not None:
            self.science.close()
        self.control.close()
