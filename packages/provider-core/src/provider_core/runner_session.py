"""Retain one control session separately from its scientific endpoint."""

from provider_core.container_guard import require_container

require_container()

import socket
import time
import errno
import threading
from dataclasses import dataclass

from provider_core.runner_protocol import LifecycleRejected, positive_seconds, receive_document, receive_greeting


@dataclass(frozen=True, slots=True)
class Retirement:
    returncode: int
    scratch_released: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OriginalWorkerExit:
    """An exit observed on its original session before local interruption began."""

    role: str
    generation: str
    operation: str
    returncode: int


class RunnerStartupStopped(InterruptedError):
    """The requested stop prevented control connection, including after known refusals."""


class RunnerStartupUnavailable(TimeoutError):
    """The SDK startup deadline expired before any successful control connection."""


class RunnerSession:
    """A synchronous I/O owner, retained by async callers through their original task.

    Construct before starting connect. request_stop retains intent before connection
    and half-closes connected control without waiting for scientific exchange.
    Once connected, the original greeting read keeps its startup deadline; join
    that setup operation before observing retirement or closing the session.
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
        self._stop_requested = threading.Event()
        self._control_state = threading.Lock()
        self._retirement_reader = threading.Lock()
        self._local_interruption = False
        self._original_exit = None
        self._connected = False
        self._observation_failure = None
        self._connect_started = False

    @property
    def control_connected(self):
        """Record successful control connect after setup joins, even after close.

        This proves neither greeting nor retirement. False does not exclude an
        operation after an arbitrary OS connect failure.
        """
        return self._connected

    def connect(self, *, deadline=None):
        if self._connect_started:
            raise ValueError("Runner session connect is single-use; retain its original setup operation.")
        self._connect_started = True
        phase_deadline = time.monotonic() + self._startup
        deadline = phase_deadline if deadline is None else min(deadline, phase_deadline)
        self._connect_control(deadline)
        try:
            with self._control_state:
                self._connected = True
                if self._stop_requested.is_set():
                    self.control.shutdown(socket.SHUT_WR)
            greeting, self.science = receive_greeting(self.control, deadline=deadline)
            if greeting["role"] != self.role or tuple(greeting["lock"]) != self.expected_lock_identity:
                self.science.close()
                self.science = None
                raise LifecycleRejected("Runner greeting does not match the admitted role and stable lifetime lock.")
            self.generation, self.operation = greeting["generation"], greeting["operation"]
            self.lock_identity = tuple(greeting["lock"])
        except BaseException as cause:
            self._observation_failure = cause
            raise

    def _connect_control(self, deadline):
        while True:
            if self._stop_requested.is_set():
                raise RunnerStartupStopped("Runner startup was stopped before connecting to its control endpoint.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunnerStartupUnavailable("Runner control endpoint was not connected within its startup budget.")
            self.control.settimeout(remaining)
            try:
                self.control.connect(self.path)
                return
            except OSError as cause:
                if cause.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise
                self._stop_requested.wait(min(0.02, max(0, deadline - time.monotonic())))

    def request_stop(self):
        with self._control_state:
            self._local_interruption = True
            if not self._stop_requested.is_set():
                self._stop_requested.set()
                if self._connected:
                    self.control.shutdown(socket.SHUT_WR)

    def mark_interruption(self):
        """Call before the first deliberate science/control close, stop or kill.

        Bindings must also call this before their own internal transport cleanup.
        It records intent without sending a stop or changing an earlier exit fact.
        """
        with self._control_state:
            self._local_interruption = True

    @property
    def original_exit(self):
        """Return the original exit fact; later cleanup cannot establish its cause."""
        with self._control_state:
            return self._original_exit

    def retire(self, *, deadline=None):
        if self.retirement is None:
            self.request_stop()
        self.observe_retirement(deadline=deadline)

    def observe_retirement(self, *, deadline=None):
        """Receive cleanup facts without causing retirement (including cap expiry)."""
        if not self._retirement_reader.acquire(blocking=False):
            raise LifecycleRejected("Runner retirement already has an original reader; join that observation instead of reading another frame.")
        try:
            self._observe_retirement(deadline)
        finally:
            self._retirement_reader.release()

    def _observe_retirement(self, deadline):
        if self._observation_failure is not None:
            raise LifecycleRejected(
                "Original runner control setup or lifecycle read failed; preserve its cause and use bounded lifecycle recovery, not a second frame read."
            ) from self._observation_failure
        if self.retirement is None:
            self.control.settimeout(self._cleanup)
            try:
                phase_deadline = time.monotonic() + self._cleanup
                effective = phase_deadline if deadline is None else min(deadline, phase_deadline)
                retirement = self._receive_retirement(effective)
                with self._control_state:
                    self.retirement = retirement
                    if retirement.reason == "exited" and not self._local_interruption:
                        self._original_exit = OriginalWorkerExit(
                            self.role, self.generation, self.operation, retirement.returncode,
                        )
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
            set(reply) not in (
                {"kind", "generation", "operation", "returncode", "scratch_released"},
                {"kind", "generation", "operation", "returncode", "scratch_released", "reason"},
            )
            or reply["kind"] != "retired" or reply["generation"] != self.generation
            or reply["operation"] != self.operation or type(reply["returncode"]) is not int
            or type(reply["scratch_released"]) is not bool
            or ("reason" in reply and reply["reason"] not in ("exited", "owner_lost", "deadline", "shutdown"))
        ):
            raise LifecycleRejected("Runner retirement is malformed or belongs to another operation.")
        return Retirement(reply["returncode"], reply["scratch_released"], reply.get("reason"))

    def close(self):
        self.mark_interruption()
        if self.science is not None:
            self.science.close()
        self.control.close()
