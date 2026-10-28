"""Retain native control I/O across cancelled or expired asynchronous waiters."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError, Future, ThreadPoolExecutor
import time

from provider_core.runner_protocol import LifecycleRejected, positive_seconds


def _observe_completion(wrapped):
    if not wrapped.cancelled():
        wrapped.exception()


async def await_original(original, *, deadline):
    """Observe caller-owned thread work, preserving its exact result or failure.

    deadline is an absolute monotonic cap, or explicit None to join without a
    new caller cap. Expiry and cancellation never cancel the original Future.
    This wait grants no resource retirement or permission to close its inputs.
    """
    if not isinstance(original, Future):
        raise TypeError("Cannot observe thread work: supply its original concurrent Future.")
    seconds = None
    if deadline is not None:
        positive_seconds(deadline)
        seconds = max(0, deadline - time.monotonic())
    if not original.done():
        wrapped = asyncio.wrap_future(original)
        wrapped.add_done_callback(_observe_completion)
        async with asyncio.timeout(seconds):
            await asyncio.wait((wrapped,))
    return original.result()


class AsyncRunnerSession:
    """Own one setup and retirement reader without owning scientific exchange.

    Call connect before observing or joining retirement. Caller wait allowances
    never cancel the original operations. The session's retirement_seconds is
    the aggregate allowance, starting at first observation or stop; providers
    must leave room inside it for stop and join after their observation wait.

    setup and reader expose original concurrent Futures retaining native results
    and exceptions. Only caller joins raise those exceptions. One dedicated
    control thread keeps setup and retirement independent of the default executor.
    The executor releases after failed setup or completed read, never waiter expiry.
    setup_failure and retirement_failure derive failures from those same Futures.
    Callers must join these operations, never cancel them or close their control
    socket while they remain active. Science adoption, closure and lost-control
    recovery belong to the provider binding. An inherited absolute not_after cap
    bounds original setup and the aggregate retirement allowance without renewal.
    setup_not_after caps only original setup, allowing a healthy warm session to
    outlive startup and retain its later retirement allowance.
    """

    def __init__(self, session, *, not_after=None, setup_not_after=None):
        if not_after is not None:
            positive_seconds(not_after)
        if setup_not_after is not None:
            positive_seconds(setup_not_after)
        self.session = session
        self._not_after = not_after
        if setup_not_after is None:
            self._setup_not_after = not_after
        elif not_after is None:
            self._setup_not_after = setup_not_after
        else:
            self._setup_not_after = min(not_after, setup_not_after)
        self._setup = self._reader = None
        self._deadline = None
        self._stop_failure = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="runner-control")

    @property
    def setup(self):
        return self._setup

    @property
    def reader(self):
        return self._reader

    @property
    def setup_failure(self):
        return self._failure(self._setup)

    @property
    def retirement_failure(self):
        return self._failure(self._reader)

    @staticmethod
    def _failure(original):
        if original is None:
            return None
        if not original.done():
            return None
        if original.cancelled():
            return LifecycleRejected("Original native control operation was cancelled; native completion remains unconfirmed.")
        return original.exception()

    @property
    def stop_failure(self):
        return self._stop_failure

    @property
    def retirement_deadline(self):
        """Share the actual aggregate deadline with provider-owned recovery."""
        return self._deadline

    def _release(self, original):
        self._executor.shutdown(wait=False)

    def _setup_completed(self, original):
        if self._failure(original) is not None:
            self._release(original)

    def _start(self, operation, completed, **kwargs):
        try:
            original = self._executor.submit(operation, **kwargs)
        except BaseException:
            self._executor.shutdown(wait=False)
            raise
        original.add_done_callback(completed)
        return original

    async def _wait(self, original, deadline):
        try:
            return await await_original(original, deadline=deadline)
        except FutureCancelledError:
            if not original.cancelled():
                raise
            raise self._failure(original) from None

    def _retirement_deadline(self):
        if self._deadline is None:
            self._deadline = time.monotonic() + self.session.retirement_seconds
            if self._not_after is not None:
                self._deadline = min(self._deadline, self._not_after)
        return self._deadline

    async def connect(self, *, wait_seconds):
        """Start or join the original setup without replacing its startup cap."""
        positive_seconds(wait_seconds)
        if self._setup is None:
            self._setup = self._start(self.session.connect, self._setup_completed, deadline=self._setup_not_after)
        deadline = time.monotonic() + wait_seconds
        if self._setup_not_after is not None:
            deadline = min(deadline, self._setup_not_after)
        return await self._wait(self._setup, deadline)

    async def observe_retirement(self, *, wait_seconds):
        """Observe without stopping; wait expiry leaves the original read active."""
        positive_seconds(wait_seconds)
        if self._setup is None:
            raise RuntimeError("Cannot observe native retirement before starting the original control setup with connect.")
        deadline = min(self._retirement_deadline(), time.monotonic() + wait_seconds)
        await self._wait(self._setup, deadline)
        if self._reader is None:
            if time.monotonic() >= self._deadline:
                raise TimeoutError("Cannot start native retirement observation: its aggregate cleanup allowance has expired.")
            self._reader = self._start(self.session.observe_retirement, self._release, deadline=self._deadline)
        return await self._wait(self._reader, deadline)

    def request_stop(self):
        """Record native interruption before stop; retain its first failure."""
        self._retirement_deadline()
        try:
            self.session.request_stop()
        except BaseException as cause:
            if self._stop_failure is None:
                self._stop_failure = cause
            raise

    async def join_retirement(self, *, wait_seconds):
        """Join the same reader; a previous stop failure does not prevent joining."""
        return await self.observe_retirement(wait_seconds=wait_seconds)
