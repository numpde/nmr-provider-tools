"""One failed phase label followed by an independently authored lifecycle read."""

import json

from tests.fakes.api import reply
from tests.fakes.attempts import observation
from tests.fakes.conditions import ConditionTransport


class PhaseProgressTransport(ConditionTransport):
    def __init__(self, phase):
        super().__init__()
        self.release.set()
        self.phase = phase
        self.failed = False
        self.phase_failure = reply(b"PRIVATE progress response", status=503)
        self.fresh_observation = reply(observation())
        self.fresh_reads = 0

    async def exchange(self, request, *, maximum_bytes, owner=None):
        received = await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)
        if request.method == "PUT":
            command = json.loads(request.content)
            if (command["phase"], command["condition_code"]) == (self.phase, None):
                if self.failed:
                    raise AssertionError("A failed phase label was resent")
                self.failed = True
                self.retirement_confirmed, self.pending_owner = False, owner
                if isinstance(self.phase_failure, BaseException):
                    raise self.phase_failure
                return self.phase_failure
        elif self.failed and self.fresh_reads == 0:
            if not self.retirement_confirmed:
                raise AssertionError("Fresh lifecycle read preceded original PUT retirement")
            self.fresh_reads += 1
            return self.fresh_observation
        return received
