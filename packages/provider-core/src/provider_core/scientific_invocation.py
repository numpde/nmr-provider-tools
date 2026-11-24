"""Retain one provider-authored sequence of calls within its live resource scope."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial

from provider_core.hosting import _join_settlement, _raise_context_failure, _start_owned_task


class ScientificInvocation[ResultT]:
    """Observe or stop the original task created by open_invocation().

    Repeated waits return its retained value or failure, without running it again.
    The owner holds that value in memory; it provides no restart recovery or
    delivery acknowledgement. Keep its enclosing hosts alive until close returns.
    """

    def __init__(self, task: asyncio.Task[ResultT]) -> None:
        self._task = task
        self._settlement: asyncio.Task[None] | None = None
        self._requested_stop = False

    async def wait(self) -> ResultT:
        """Wait for the original sequence; cancelling this wait leaves it running."""

        return await asyncio.shield(self._task)

    async def _settle(self, *, requested_stop: bool) -> None:
        try:
            await self._task
        except asyncio.CancelledError:
            if not (requested_stop):
                raise

    def request_stop(self) -> None:
        """Request cancellation once; close() still owns the required settlement."""

        if not self._task.done() and not self._task.cancelling():
            self._requested_stop = self._task.cancel()

    async def close(self) -> None:
        """Stop once and join cleanup before propagating observer cancellation.

        ScientificCall settles its active analysis when cancelled. Independent
        cleanup failures still propagate; the cancellation requested here alone
        is expected shutdown. Repeated close calls join the same settlement.
        """

        if self._settlement is None:
            self.request_stop()
            self._settlement = _start_owned_task(operation=partial(self._settle, requested_stop=self._requested_stop))

        await _join_settlement(task=self._settlement)


@asynccontextmanager
async def open_invocation[ResultT](operation: Callable[[], Awaitable[ResultT]]) -> AsyncIterator[ScientificInvocation[ResultT]]:
    """Start one sequence and settle it before leaving its enclosing resource scope.

    The supplied coroutine owns its resources through cancellation, as composed
    ScientificCall instances do. Nest this scope inside their host contexts.
    An observer may time out and rejoin via wait(); leaving this scope instead
    cancels unfinished work and joins it, even through repeated cancellation.
    A new context starts new work. No sequence-level retries are performed.
    """
    invocation = ScientificInvocation(_start_owned_task(operation=operation))

    try:
        yield invocation
    except BaseException as original:
        try:
            await invocation.close()
        except BaseException as cleanup:
            _raise_context_failure(original=original, cleanup=cleanup)

        raise
    else:
        await invocation.close()
