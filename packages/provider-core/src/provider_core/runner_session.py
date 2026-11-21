"""Retain one control session separately from its scientific endpoint."""

from provider_core.container_guard import require_container

require_container()

import socket
import signal
import time
import errno
import threading
from dataclasses import dataclass

from provider_core.runner_protocol import LifecycleRejected, receive_document, receive_greeting
from provider_core.tools import require_positive_number
from provider_core.type_requirements import require_type


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
    startup_not_after optionally retains an earlier time.monotonic() deadline
    across lock checks and deferred connect. It caps setup, never later retirement.
    """

    def __init__(
        self,
        path,
        *,
        role,
        expected_lock_identity,
        startup_seconds,
        retirement_seconds,
        startup_not_after=None,
    ):
        require_positive_number(startup_seconds)
        require_positive_number(retirement_seconds)

        if startup_not_after is not None:
            require_positive_number(startup_not_after)

        (self.path, self._startup, self._cleanup) = (str(path), startup_seconds, retirement_seconds)
        self._startup_not_after = startup_not_after

        (self.role, self.expected_lock_identity) = (role, tuple(expected_lock_identity))
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
    def retirement_seconds(self):
        """Return the configured aggregate allowance for native observation and stop."""

        return self._cleanup

    @property
    def control_connected(self):
        """Record successful control connect after setup joins, even after close.

        This proves neither greeting nor retirement. False does not exclude an
        operation after an arbitrary OS connect failure.
        """

        return self._connected

    def connect(self, *, deadline=None):
        if self._connect_started:
            raise ValueError(f"{self._connect_started=} should be False before starting control setup")

        self._connect_started = True

        phase_deadline = time.monotonic() + self._startup
        deadline = phase_deadline if deadline is None else min(deadline, phase_deadline)

        if self._startup_not_after is not None:
            deadline = min(deadline, self._startup_not_after)

        self._connect_control(deadline)

        try:
            with self._control_state:
                self._connected = True

                if self._stop_requested.is_set():
                    self.control.shutdown(socket.SHUT_WR)

            (greeting, self.science) = receive_greeting(self.control, deadline=deadline)

            if (greeting["role"] != self.role) or (tuple(greeting["lock"]) != self.expected_lock_identity):
                self.science.close()
                self.science = None

                raise LifecycleRejected(
                    f"{greeting['role']=} should match {self.role=} and "
                    f"{tuple(greeting['lock'])=} should match {self.expected_lock_identity=}"
                )

            (self.generation, self.operation) = (greeting["generation"], greeting["operation"])
            self.lock_identity = tuple(greeting["lock"])
        except BaseException as cause:
            self._observation_failure = cause

            raise

    def _connect_control(self, deadline):
        while True:
            startup_stopped = self._stop_requested.is_set()

            if startup_stopped:
                raise RunnerStartupStopped(f"{startup_stopped=} should be False before connecting to the control endpoint")

            startup_remaining_seconds = deadline - time.monotonic()

            if startup_remaining_seconds <= 0:
                raise RunnerStartupUnavailable(f"{startup_remaining_seconds=} should be positive before connecting to the control endpoint")

            self.control.settimeout(startup_remaining_seconds)

            try:
                self.control.connect(self.path)

                return
            except OSError as cause:
                if not (cause.errno in (errno.ENOENT, errno.ECONNREFUSED)):
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

        retirement_reader_acquired = self._retirement_reader.acquire(blocking=False)

        if not (retirement_reader_acquired):
            raise LifecycleRejected(f"{retirement_reader_acquired=} should be True before observing retirement")

        try:
            self._observe_retirement(deadline)
        finally:
            self._retirement_reader.release()

    def _observe_retirement(self, deadline):
        # repo-check: allow-literal-guard-error -- The chained original failure supplies evidence; another frame read cannot recover a partial read
        if not (self._observation_failure is None):
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

                    if (retirement.reason == "exited") and (not self._local_interruption):
                        self._original_exit = OriginalWorkerExit(
                            self.role,
                            self.generation,
                            self.operation,
                            retirement.returncode,
                        )
            except Exception as cause:
                # A partial frame cannot restart at its remaining bytes.
                # Retain this owner until the binding's proof session settles it.
                self._observation_failure = cause

                raise

        if not (self.retirement.scratch_released):
            raise LifecycleRejected(
                f"{self.retirement.scratch_released=} should be True after the child stops",
                returncode=self.retirement.returncode,
                scratch_released=False
            )

    def _receive_retirement(self, deadline):
        retirement_message = receive_document(self.control, deadline=deadline)
        retirement_fields = set(retirement_message)
        expected_fields = {"kind", "generation", "operation", "returncode", "scratch_released", "reason"}

        if retirement_fields != expected_fields:
            raise LifecycleRejected(f"{retirement_fields=} should match {expected_fields=}")

        if retirement_message["kind"] != (expected_kind := "retired"):
            raise LifecycleRejected(f"{retirement_message['kind']=} should match {expected_kind=}")

        if retirement_message["generation"] != self.generation:
            raise LifecycleRejected(f"{retirement_message['generation']=} should match {self.generation=}")

        if retirement_message["operation"] != self.operation:
            raise LifecycleRejected(f"{retirement_message['operation']=} should match {self.operation=}")

        require_type(
            (retirement_returncode := retirement_message["returncode"]),
            int,
            error_factory=LifecycleRejected,
        )

        require_type(
            (retirement_scratch_released := retirement_message["scratch_released"]),
            bool,
            error_factory=LifecycleRejected,
        )

        if not (retirement_message["reason"] in (expected_reasons := ("exited", "owner_lost", "deadline", "shutdown"))):
            raise LifecycleRejected(f"{retirement_message['reason']=} should be one of {expected_reasons=}")

        (min_returncode, max_returncode) = (1 - signal.NSIG, 255)

        if not (min_returncode <= retirement_returncode <= max_returncode):
            raise LifecycleRejected(f"{retirement_returncode=} should be between {min_returncode=} and {max_returncode=}")

        return Retirement(retirement_message["returncode"], retirement_message["scratch_released"], retirement_message["reason"])

    def close(self):
        self.mark_interruption()

        if self.science is not None:
            self.science.close()

        self.control.close()
