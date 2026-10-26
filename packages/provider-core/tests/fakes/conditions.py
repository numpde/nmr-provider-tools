"""Awaited condition ports and a controllable progress transport."""

import asyncio
import json

from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.execution import Session
from tests.fakes.job_input import response


class ReportingSession(Session):
    def __init__(self, store):
        super().__init__(store)
        self.reporters = {}
        self.conditions = {"prepare": ("waiting_capacity", None), "execute": ("waiting_capacity", None)}

    async def report(self, phase, report_condition):
        self.reporters[phase] = report_condition
        for condition in self.conditions[phase]:
            await report_condition(condition)

    async def prepare(self, acknowledged, instructions, *, report_condition):
        await self.report("prepare", report_condition)
        return await super().prepare(acknowledged, instructions, report_condition=report_condition)

    async def execute(self, prepared, *, report_condition):
        await self.report("execute", report_condition)
        return await super().execute(prepared, report_condition=report_condition)


class SwallowingSession(ReportingSession):
    async def report(self, phase, report_condition):
        try:
            await super().report(phase, report_condition)
        except (Exception, asyncio.CancelledError) as cause:
            self.caught = cause


class ConditionTransport(Transport):
    def __init__(self):
        super().__init__()
        self.block_condition = "waiting_capacity"
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.in_flight = 0
        self.maximum_in_flight = 0
        self.failure = None
        self.reads = 0
        self.block_read = None
        self.read_blocked = asyncio.Event()
        self.release_read = asyncio.Event()

    async def exchange(self, request, *, maximum_bytes, owner=None):
        if not self.retirement_confirmed:
            self.pending_owner = owner
        self.calls.append((request, maximum_bytes))
        self.in_flight += 1
        self.maximum_in_flight = max(self.maximum_in_flight, self.in_flight)
        try:
            if request.method == "PUT":
                command = json.loads(request.content)
                if command["condition_code"] == self.block_condition:
                    self.blocked.set()
                    await self.release.wait()
                    if self.failure is not None:
                        raise self.failure
                return reply(progress_response(phase=command["phase"], condition_code=command["condition_code"]))
            if request.url.path.endswith("/input"):
                return reply(response())
            self.reads += 1
            if self.reads == self.block_read:
                self.read_blocked.set()
                await self.release_read.wait()
            return reply(observation())
        finally:
            self.in_flight -= 1
