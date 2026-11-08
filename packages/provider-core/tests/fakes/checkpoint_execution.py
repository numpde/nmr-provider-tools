"""Recording analysis and relay ports for live lifecycle tests."""

import asyncio
from pathlib import Path

from provider_core.delivery_client import DeliveryEndpoint, DeliveryRejected, DeliveryUnavailable
from provider_core.delivery_store import DeliveryFull
from tests.fakes.attempt_store import scope
from tests.fakes.execution import Session, live_attempt


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

class CausalDelivery(RecordingDelivery):
    """Hold exact producer submissions until an external relay decision arrives.

    This is an in-memory port. A response classified as accepted represents a
    valid local ACK, not a Jobs API publication receipt. A lost ACK can follow
    possible local storage without conferring producer-side acceptance.
    """

    def __init__(self):
        super().__init__()
        self.offers = []
        self._offers_changed = asyncio.Condition()
        self._answers = asyncio.Queue()
        self._answered = 0
        self._tearing_down = False

    @property
    def awaiting_answer(self):
        return not self._tearing_down and len(self.offers) > self._answered

    async def wait_for_offers(self, count):
        async with self._offers_changed:
            await self._offers_changed.wait_for(lambda: len(self.offers) >= count)

    def answer(self, disposition):
        if disposition not in ("accepted", "full", "retryable", "lost", "rejected"):
            raise ValueError("Unknown causal delivery disposition")
        if not self.awaiting_answer:
            raise RuntimeError("No original terminal submission awaits a decision")
        self._answered += 1
        self._answers.put_nowait(disposition)

    def release_for_teardown(self):
        """Unblock disposable tasks only after their behavioral verdict."""
        self._tearing_down = True
        self._answers.put_nowait("accepted")

    async def submit(self, item):
        async with self._offers_changed:
            self.offers.append(item)
            self._offers_changed.notify_all()
        # A new offer after emergency teardown is not a proof of delivery.
        if self._tearing_down:
            return await super().submit(item)
        disposition = await self._answers.get()
        if disposition == "accepted":
            return await super().submit(item)
        if disposition == "full":
            raise DeliveryFull("The local delivery queue has no available capacity")
        if disposition == "retryable":
            failure = DeliveryUnavailable("The local delivery service is temporarily unavailable", status=503)
            self.last_failure = failure
            raise failure
        if disposition == "lost":
            # The relay may have accepted these immutable bytes; the producer
            # saw no verified ACK and may not infer or replace the outcome.
            self.items.append(item)
            failure = DeliveryUnavailable("The exact local acknowledgement was lost")
            self.last_failure = failure
            raise failure
        if disposition == "rejected":
            raise DeliveryRejected("The relay response did not match the selected terminal")
        raise AssertionError("A causal delivery decision was not consumed")

    def can_retry(self, failure):
        # Follow the real client: only the exact owned, retired, retryable 503
        # failure authorizes another unchanged submission.
        return (failure is self.last_failure and type(failure) is DeliveryUnavailable
                and failure.status == 503 and self.retirement_confirmed)
