"""Publish one provider's complete offering snapshot through one exchange owner.

Hello replaces the API directory entry atomically. Offerings therefore share a
publisher even when their workers start, fail or recover independently. The
provider supplies its current catalog or ready subset; retries sample it again.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from time import monotonic

from provider_core.api_errors import ApiFailure
from provider_core.tools import require_positive_number


class HelloPublisher:
    """Own publication ordering without deciding which offerings are usable.

    retry_policy, when supplied, must authorize a fresh snapshot by returning a
    positive delay, or raise the failure. It runs after transport retirement.
    The policy owns authorization: ordinary recovery uses
    ProviderAPI.can_refresh_hello; deployments may separately permit catalog
    refusal refresh without claiming that their workers became unavailable.
    on_accepted receives the actual API receipt. Neither callback grants job
    admission or changes local readiness. An explicit owner lets enclosing
    resource cleanup retire the same exchange after the publisher task exits.
    """

    def __init__(
        self,
        api,
        *,
        provider_ref,
        display_name,
        description,
        offerings,
        stopping,
        retry_policy=None,
        on_accepted=None,
        owner=None,
    ):
        self.api = api
        self.owner = self if owner is None else owner
        self.provider_ref = provider_ref
        self.display_name = display_name
        self.description = description
        self.offerings = offerings
        self.stopping = stopping
        self.retry_policy = retry_policy
        self.on_accepted = on_accepted
        self.withdrawal_required = False
        self._announced = None
        self._publication = asyncio.Lock()

    @property
    def retirement_confirmed(self):
        return self.api.retirement_confirmed

    async def _send(self, offerings):
        # An accepted snapshot can outlive its missing acknowledgement.
        # Retain withdrawal duty before sending any nonempty inventory.
        if offerings:
            self.withdrawal_required = True

        accepted = await self.api.publish_hello(
            provider_ref=self.provider_ref,
            display_name=self.display_name,
            description=self.description,
            analysis_offerings=offerings,
            owner=self.owner,
        )

        self._announced = offerings
        self.withdrawal_required = bool(offerings)

        if self.on_accepted is not None:
            self.on_accepted(accepted)

        return accepted

    async def refresh(self, *, retry_wait=None):
        """Publish current state; each permitted retry samples a fresh snapshot.

        An optional retry_wait receives the nominal seconds and the server's
        minimum_delay_seconds separately, so jitter cannot shorten that floor.
        It returns True when interrupted; refresh then returns without another
        send. The default wait uses the larger delay and this publisher's stop.
        """

        async with self._publication:
            while not self.stopping.is_set():
                try:
                    return await self._send(self.offerings())
                except ApiFailure as failure:
                    await self.retire()

                    if not (self.retry_policy is not None):
                        raise

                    seconds = self.retry_policy(failure)

                    require_positive_number(seconds)

                    if retry_wait is None:
                        if failure.retry_after_seconds is not None:
                            seconds = max(seconds, failure.retry_after_seconds)

                        interrupted = await self.pause(seconds)
                    else:
                        interrupted = await retry_wait(
                            seconds,
                            minimum_delay_seconds=failure.retry_after_seconds,
                        )

                    if interrupted:
                        return None

    async def run(self, *, refresh_seconds, poll_seconds):
        """Refresh on inventory changes or heartbeat, with only one send in flight."""
        require_positive_number(refresh_seconds)
        require_positive_number(poll_seconds)
        next_refresh = monotonic()

        if self._announced is not None:
            next_refresh += refresh_seconds

        while not self.stopping.is_set():
            if (self.offerings() != self._announced) or (monotonic() >= next_refresh):
                await self.refresh()
                next_refresh = monotonic() + refresh_seconds

                # A worker may have changed while the old snapshot was in flight.
                # Recheck before sleeping so its replacement is sent immediately.
                continue

            await self.pause(min(poll_seconds, max(0, next_refresh - monotonic())))

    async def withdraw(self):
        """Replace uncertain or acknowledged availability with an empty snapshot.

        The service first stops and joins its publisher loop. Retirement confirms
        the old exchange ended before withdrawal can supersede it. A failed
        withdrawal retains the duty; it is not evidence of API acceptance.
        """

        async with self._publication:
            if self.withdrawal_required:
                await self.retire()

                return await self._send(())

    async def retire(self):
        """Settle only this publisher's original transport exchange."""
        await self.api.retire(owner=self.owner)

    async def pause(self, seconds):
        """Wait for the next observation or stop without admitting another send."""

        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)
        # silent-defaults: allow-exception-outcome -- Expiry schedules another observation; it does not report a successful publication.
        except TimeoutError:
            return False

        return True
