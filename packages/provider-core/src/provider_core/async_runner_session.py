"""Retain native control I/O across cancelled or expired asynchronous waiters."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError, Future, ThreadPoolExecutor
import time

from provider_core.runner_protocol import LifecycleRejected
from provider_core.task_boundary import contain_task_exit
from provider_core.tools import require_positive_number
from provider_core.type_requirements import require_instance


def _observe_completion(wrapped):
    if not wrapped.cancelled():
        wrapped.exception()


async def await_original(original, *, deadline):
    """Observe caller-owned thread work, preserving its exact result or failure.

    deadline is an absolute monotonic cap, or explicit None to join without a
    new caller cap. Expiry and cancellation never cancel the original Future.
    This wait grants no resource retirement or permission to close its inputs.
    """

    require_instance(original, Future)

    seconds = None

    if deadline is not None:
        require_positive_number(deadline)
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

    observe_exit first owns an asynchronous readiness wait, then a bounded read
    of the supervisor's exit notification. Healthy residence has no reader thread
    or retirement deadline. Cleanup wakes that same owner and joins its read
    before reading the final retirement receipt.
    """

    def __init__(self, session, *, not_after=None, setup_not_after=None):
        if not_after is not None:
            require_positive_number(not_after)

        if setup_not_after is not None:
            require_positive_number(setup_not_after)

        self.session = session
        self._not_after = not_after

        if setup_not_after is None:
            self._setup_not_after = not_after
        elif not_after is None:
            self._setup_not_after = setup_not_after
        else:
            self._setup_not_after = min(not_after, setup_not_after)

        self._setup = self._reader = None
        self._exit_task = self._exit_reader = None
        self._exit_wakeup = asyncio.Event()
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
    def exit_reader(self):
        return self._exit_reader

    @property
    def setup_failure(self):
        return self._failure(self._setup)

    @property
    def retirement_failure(self):
        return self._failure(self._reader)

    @property
    def exit_failure(self):
        return self._failure(self._exit_task) or self._failure(self._exit_reader)

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
        require_positive_number(wait_seconds)

        if self._setup is None:
            self._setup = self._start(self.session.connect, self._setup_completed, deadline=self._setup_not_after)

        deadline = time.monotonic() + wait_seconds

        if self._setup_not_after is not None:
            deadline = min(deadline, self._setup_not_after)

        return await self._wait(self._setup, deadline)

    async def observe_retirement(self, *, wait_seconds):
        """Observe without stopping; wait expiry leaves the original read active."""
        require_positive_number(wait_seconds)

        if self._setup is None:
            raise RuntimeError(f"{self._setup=} should be a Future before observing retirement")

        deadline = min(self._retirement_deadline(), time.monotonic() + wait_seconds)

        await self._wait(self._setup, deadline)

        if self._exit_task is not None:
            # Switch the passive readiness wait to the fixed cleanup deadline
            self._exit_wakeup.set()
            try:
                await self._join_exit_reader(deadline)
            except BaseException as cause:
                if (self._exit_reader is not None) or (self._failure(self._exit_task) is not cause):
                    raise

                # Failed readiness consumed no bytes; cleanup still owns its read

        if self._reader is None:
            observation_started_at = time.monotonic()

            if observation_started_at >= self._deadline:
                raise TimeoutError(f"{observation_started_at=} should precede {self._deadline=}")

            self._reader = self._start(self.session.observe_retirement, self._release, deadline=self._deadline)

        return await self._wait(self._reader, deadline)

    async def _watch_exit(self):
        loop = asyncio.get_running_loop()
        descriptor = self.session.control.fileno()

        # Readiness consumes no bytes and owns no idle thread or cleanup budget
        loop.add_reader(descriptor, self._exit_wakeup.set)

        try:
            await self._exit_wakeup.wait()
        finally:
            loop.remove_reader(descriptor)

        frame_deadline = time.monotonic() + self.session.retirement_seconds

        if self._deadline is not None:
            frame_deadline = min(frame_deadline, self._deadline)

        if self._not_after is not None:
            frame_deadline = min(frame_deadline, self._not_after)

        self._exit_reader = self._start(self.session.observe_exit, self._exit_completed, deadline=frame_deadline)

    def _exit_completed(self, original):
        if self._failure(original) is not None:
            self._release(original)

    async def _join_exit_reader(self, deadline):
        seconds = None if deadline is None else max(0, deadline - time.monotonic())

        async with asyncio.timeout(seconds):
            await asyncio.shield(self._exit_task)

        return await self._wait(self._exit_reader, deadline)

    async def observe_exit(self, *, wait_seconds):
        """Wait for original exit evidence without starting retirement.

        Caller timeout or cancellation leaves the single passive monitor owned.
        Once readable, a bounded frame read retains exit independently of scratch
        cleanup. None means a final receipt without exit observed before local
        interruption; it does not establish why the worker exited.
        """
        require_positive_number(wait_seconds)

        if self._setup is None:
            raise RuntimeError(f"{self._setup=} should be a Future before observing worker exit")

        if self.session.original_exit is not None:
            return self.session.original_exit

        deadline = time.monotonic() + wait_seconds

        await self._wait(self._setup, deadline)

        try:
            if self._reader is not None:
                await self._wait(self._reader, deadline)
            else:
                if self._exit_task is None:
                    self._exit_task = asyncio.create_task(contain_task_exit(self._watch_exit))

                    self._exit_task.add_done_callback(_observe_completion)

                await self._join_exit_reader(deadline)
        except TimeoutError as cause:
            # Caller expiry grants another wait; a consumed partial frame does not
            for original in (self._exit_reader, self._reader):
                if (original is not None) and original.done() and (not original.cancelled()) and (original.exception() is cause):
                    raise LifecycleRejected(f"Cannot observe {self.session.operation=}: its lifecycle frame read expired") from cause

            raise

        return self.session.original_exit

    def request_stop(self):
        """Record native interruption before stop; retain its first failure."""
        self._retirement_deadline()
        self._exit_wakeup.set()

        try:
            self.session.request_stop()
        except BaseException as cause:
            if self._stop_failure is None:
                self._stop_failure = cause

            raise

    async def join_retirement(self, *, wait_seconds):
        """Join the same reader; a previous stop failure does not prevent joining."""

        return await self.observe_retirement(wait_seconds=wait_seconds)
