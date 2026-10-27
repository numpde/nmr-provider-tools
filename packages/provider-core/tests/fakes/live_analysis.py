"""A live scientific owner with observable, independently held retirement."""

import asyncio

from provider_core.analysis import AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence


class LiveSession:
    def __init__(self):
        self.instructions = None
        self.events = []
        self.handle = PreparedAnalysis(ProtectedEvidence("fixture.prepared.v1", b"prepared"))
        self.retiring, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.content = b"result"

    async def prepare(self, acknowledged, instructions, *, report_condition):
        self.events.append("prepare")
        self.instructions = instructions
        return self.handle

    async def execute(self, prepared, *, report_condition):
        self.events.append("execute")
        if prepared is not self.handle:
            raise AssertionError("Foreign scientific handle")
        return AnalysisSuccess("result.v1", self.content, ProtectedEvidence("fixture.outcome.v1", b"evidence"))

    async def retire(self):
        self.retiring.set()
        await self.release.wait()
        self.events.append("retire")
