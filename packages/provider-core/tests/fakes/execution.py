"""Recording async session: proves core ordering, not actual worker retirement."""

import asyncio
from pathlib import Path

from provider_core.analysis import AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence, acknowledge_start
from provider_core.checkpoint_store import CheckpointLimits, CheckpointStore
from provider_core.ownership import AttemptClaims
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF


async def unexpected_condition(_condition_code):
    raise AssertionError("This scientific fixture does not report conditions")


async def acknowledged_store(directory):
    pending = record()
    acknowledged = acknowledge_start(pending, admission())
    store = CheckpointStore(Path(directory), scope=scope(), limits=CheckpointLimits(2, 300000), operation_seconds=1)
    await store.open()
    await store.retain_start(pending)
    await store.acknowledge_start(pending, acknowledged.admission)
    claim = AttemptClaims().claim_start(pending)
    owner = object()
    claim.begin(owner)
    claim.bind_attempt(scope(), ATTEMPT_REF, owner=owner)
    claim.finish(owner)
    return store, acknowledged, claim


class Session:
    def __init__(self, store):
        self.store = store
        self.handle = PreparedAnalysis(ProtectedEvidence("fixture.prepared_input.v1", b"scripted candidate"))
        self.events = []
        self.preparation = self.handle
        self.outcome = AnalysisSuccess("result.v1", b"result bytes", ProtectedEvidence("evidence.v1", b"private evidence"))
        self.retirement_failure = None

    # Match the required session port; this recording fixture emits no conditions.
    async def prepare(self, acknowledged, instructions, *, report_condition):  # noqa: ARG002
        self.events.append(("prepare", (await self.store.inventory())[0], instructions))
        if acknowledged.start.input != instructions.identity:
            raise AssertionError("substituted input")
        return self.preparation

    # Match the required session port; this recording fixture emits no conditions.
    async def execute(self, prepared, *, report_condition):  # noqa: ARG002
        if prepared is not self.handle:
            raise AssertionError("foreign prepared handle")
        self.events.append(("execute", (await self.store.inventory())[0]))
        return self.outcome

    async def retire(self):
        self.events.append(("retire",))
        if self.retirement_failure is not None:
            raise self.retirement_failure


class PausedSession(Session):
    """Expose an in-flight phase so policy tests observe state before completion."""

    def __init__(self, store, phase):
        super().__init__(store)
        self.phase = phase
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    async def _pause(self, phase):
        if self.phase == phase:
            self.paused.set()
            await self.release.wait()

    async def prepare(self, acknowledged, instructions, *, report_condition):
        prepared = await super().prepare(acknowledged, instructions, report_condition=report_condition)
        await self._pause("prepare")
        return prepared

    async def execute(self, prepared, *, report_condition):
        outcome = await super().execute(prepared, report_condition=report_condition)
        await self._pause("execute")
        return outcome


async def execute_with_watchdog(executor, acknowledged):
    """Keep a polling regression from hanging tests on a paused session."""
    task = asyncio.create_task(executor.run(acknowledged))
    try:
        await asyncio.wait((task,), timeout=3)
        if not task.done():
            raise TimeoutError("Test executor did not finish; cancel and settle its original task")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait((task,), timeout=1)
        if not task.done():
            raise TimeoutError("Test executor did not settle after cancellation")
        if not task.cancelled():
            task.exception()
