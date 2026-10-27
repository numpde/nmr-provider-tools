"""An Attempt owner observes an actual child exit in its active science phase."""

import asyncio
import os
from dataclasses import replace

from provider_core.analysis import ObservedWorkerCrash
from provider_core.attempt_record import ProtectedEvidence
from tests.fakes.live_analysis import LiveSession
from tests.fakes.runner import worker_pid


class CrashingSession(LiveSession):
    def __init__(self, native, crash_phase):
        super().__init__()
        self.native, self.crash_phase = native, crash_phase
        self.acknowledged = None
        self.claimed_phase = crash_phase
        self.claimed_owner = self
        self.clone_acknowledgement = False
        self.interrupt_first = False
        self.begin_crash, self.crash_ready = asyncio.Event(), asyncio.Event()
        self.begin_crash.set()

    def _observe_exit(self):
        pid = worker_pid(self.native.science, b"H")
        if self.interrupt_first:
            self.native.mark_interruption()
        os.kill(pid, 9)
        self.native.observe_retirement()

    async def _crash(self):
        await self.begin_crash.wait()
        await asyncio.to_thread(self._observe_exit)
        acknowledged = self.acknowledged
        if self.clone_acknowledgement:
            acknowledged = replace(acknowledged)
        failure = ObservedWorkerCrash.from_session(
            acknowledged, phase=self.claimed_phase, owner=self.claimed_owner, runner=self.native,
            evidence=ProtectedEvidence("fixture.crash.v1", b"observed original native exit"),
            cause=EOFError("PRIVATE_SCIENTIFIC_CONTEXT"),
        )
        self.crash_ready.set()
        raise failure

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.acknowledged = acknowledged
        prepared = await super().prepare(acknowledged, instructions, report_condition=report_condition)
        if self.crash_phase == "preparing":
            await self._crash()
        return prepared

    async def execute(self, prepared, *, report_condition):
        self.events.append("execute")
        if prepared is not self.handle:
            raise AssertionError("Foreign scientific handle")
        await self._crash()

    async def retire(self):
        if self.native.retirement is None:
            await asyncio.to_thread(self.native.retire)
        else:
            await asyncio.to_thread(self.native.observe_retirement)
        await super().retire()
