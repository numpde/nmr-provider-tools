"""Wire replies and bounded cleanup for live runtime controller tests."""

import asyncio

from tests.fakes.api import reply
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.execution import Session
from tests.fakes.job_input import response


def lifecycle_replies():
    return (
        reply(start_response()),
        reply(observation()),
        reply(response()),
        reply(progress_response()),
        reply(observation()),
        reply(progress_response(phase="running")),
        reply(observation()),
        reply(observation())
    )


class SingleRetirementSession(Session):
    """A successful retirement is final, rather than an idempotent operation."""

    async def retire(self):
        if retirement_requested := any((event[0] == "retire") for event in self.events):
            raise RuntimeError(f"{retirement_requested=} should be False")

        await super().retire()


async def cancel_task(task):
    """Bound cleanup of the watchdog task even after an assertion fails."""

    if not task.done():
        task.cancel()
        await asyncio.wait((task,), timeout=2)

    if not (task_settled := task.done()):
        raise TimeoutError(f"{task_settled=} should be True after cancellation")

    if not task.cancelled():
        task.exception()
