"""Route process termination requests to the provider's stop policy."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from contextlib import contextmanager
import signal

from provider_core.failure_collection import FailureCollector


@contextmanager
def termination_signals(request_stop):
    """Own SIGINT and SIGTERM for a process entrypoint, restoring its handlers.

    The callback runs on the asyncio loop, outside the interrupted Python frame.
    It selects startup cancellation or admission stop; this context does not
    cancel tasks, retire resources, or release lifetime exclusion. Use it at the
    main-thread entrypoint before installing any asyncio handlers for these signals.
    """
    loop = asyncio.get_running_loop()
    previous = {}
    failures = FailureCollector(message="Provider execution and signal restoration failed")

    def receive(_number, _frame):
        loop.call_soon_threadsafe(request_stop)

    with failures.capture():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, receive)

        yield

    for (number, handler) in reversed(tuple(previous.items())):
        with failures.capture():
            signal.signal(number, handler)

    failures.raise_if_any()
