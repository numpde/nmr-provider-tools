"""Wire replies and bounded cleanup for live runtime controller tests."""

import asyncio

from tests.fakes.api import reply
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.execution import Session
from tests.fakes.job_input import response


def lifecycle_replies():
    return (reply(start_response()), reply(observation()), reply(response()), reply(progress_response()),
            reply(observation()), reply(progress_response(phase="running")), reply(observation()),
            reply(observation()))


class SingleRetirementSession(Session):
    """A successful retirement is final, rather than an idempotent operation."""

    async def retire(self):
        if any(event[0] == "retire" for event in self.events):
            raise RuntimeError("Scripted session was already retired; do not release its resources twice")
        await super().retire()


async def cancel_task(task):
    """Bound cleanup of the watchdog task even after an assertion fails."""
    if not task.done():
        task.cancel()
        await asyncio.wait((task,), timeout=2)
    if not task.done():
        raise TimeoutError("Test task did not settle after cancellation")
    if not task.cancelled():
        task.exception()
