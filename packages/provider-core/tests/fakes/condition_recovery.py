"""Fail one chosen condition, retaining its exact signed requests and retirement."""

import asyncio
import json

from tests.fakes.api import reply
from tests.fakes.conditions import ConditionTransport


class ConditionOutageTransport(ConditionTransport):
    def __init__(self, *, first_failed_put):
        super().__init__()
        self.release.set()
        self.first_failed_put = first_failed_put
        self.puts = 0
        self.condition_body = None
        self.condition_requests = []
        self.faults = iter((reply(b"PRIVATE condition response", status=503),))
        self.retirement_entered = asyncio.Event()
        self.release_retirement = asyncio.Event()
        self.release_retirement.set()
        self.read_after_failure = asyncio.Event()
        self.followup_observation = None

    async def exchange(self, request, *, maximum_bytes, owner=None):
        received = await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
        if request.method == "PUT":
            self.puts += 1
            if self.puts == self.first_failed_put:
                self.condition_body = request.content
            if request.content == self.condition_body:
                self.condition_requests.append(request)
                fault = next(self.faults, None)
                if fault is not None:
                    self.retirement_confirmed, self.pending_owner = False, owner
                    if isinstance(fault, BaseException):
                        raise fault
                    return fault
        elif self.condition_body is not None:
            if not self.retirement_confirmed:
                raise AssertionError("Monitor read preceded original condition PUT retirement")
            self.read_after_failure.set()
            if self.followup_observation is not None:
                return self.followup_observation
        return received

    async def retire(self, *, owner=None):
        self.retirement_entered.set()
        await self.release_retirement.wait()
        await super().retire(owner=owner)

    @property
    def condition(self):
        return json.loads(self.condition_body)["condition_code"]
