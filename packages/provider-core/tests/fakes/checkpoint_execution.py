"""Recording execution and relay ports around the real compact checkpoint owner."""

import asyncio
from pathlib import Path

from provider_core.delivery_client import DeliveryEndpoint
from tests.fakes.attempt_store import scope
from tests.fakes.execution import Session, acknowledged_store


class CheckpointSession(Session):
    def __init__(self, store):
        super().__init__(store)
        self.retirement_started = asyncio.Event()

    async def retire(self):
        self.retirement_started.set()
        await super().retire()


class RecordingDelivery:
    """Record exact bodies at the receiving boundary, outside producer ownership."""

    def __init__(self):
        self.endpoint = DeliveryEndpoint(scope().api_origin, scope().provider_ref, Path("/tmp/relay.sock"))
        self.items = []
        self.retirement_confirmed = True
        self.last_failure = None
        self.accepted = asyncio.Event()

    async def submit(self, item):
        self.items.append(item)
        self.accepted.set()
        return item.acceptance()

    async def retire(self):
        self.retirement_confirmed = True

    def can_retry(self, failure):
        return False
