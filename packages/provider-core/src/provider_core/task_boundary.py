"""Contain process-control exceptions inside their original asynchronous task."""

from provider_core.container_guard import require_container

require_container()


async def contain_task_exit(operation, /, *args, **kwargs):
    """Keep an owned operation's process-control exception from aborting the loop.

    Only KeyboardInterrupt and SystemExit are grouped; their exact objects remain
    available to the owner. This grants no outcome, retirement or retry authority.
    """
    try:
        return await operation(*args, **kwargs)
    except (KeyboardInterrupt, SystemExit) as cause:
        raise BaseExceptionGroup("Owned asynchronous task raised a process-control exception", (cause,)) from None
