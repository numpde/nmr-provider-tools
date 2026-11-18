"""Drain one configured provider's immutable terminal requests independently.

ProviderAPI owns signing, matching receipts and exact-request replay permission.
This owner supplies pacing and pending-only storage; it never runs science or
manufactures producer Attempt records to reuse the old file-store publisher.
"""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

import asyncio
import random
from collections.abc import Callable
from typing import TYPE_CHECKING

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.attempt_record import TerminalOperation
from provider_core.response_json import ResponseRejected
from provider_core.tools import require_positive_number

if TYPE_CHECKING:
    from provider_core.delivery_client import DeliveryEndpoint
    from provider_core.delivery_ipc import DeliveryInbox
    from provider_core.delivery_store import DeliveryOutcome


class DeliveryWorkerFailed(RuntimeError):
    """Retain independent API/storage and retirement causes for the operator."""

    def __init__(
        self,
        *,
        cause: BaseException | None,
        api_failure: ApiFailure | None,
        cleanup_failures: tuple[BaseException, ...],
        api_receipt_received: bool,
    ) -> None:
        """Keep publication and cleanup failures separate, including receipt state.

        A received API receipt does not prove that local queue removal completed.
        The retained causes let the operator distinguish those outcomes.
        """
        super().__init__("Delivery stopped for this provider binding; preserve its pending queue and settle the retained failure before restarting delivery.")
        (self.cause, self.api_failure) = (cause, api_failure)
        self.cleanup_failures = cleanup_failures
        self.api_receipt_received = api_receipt_received


def require_delivery_retry_settings(initial_seconds: float, maximum_seconds: float) -> None:
    """Require finite positive retry delays, with initial no greater than maximum.

    Services and standalone workers use the same admission before publication.
    Wrong numeric types raise TypeError; invalid values or ordering raise ValueError.
    """

    require_positive_number(initial_seconds)
    require_positive_number(maximum_seconds)

    if not (initial_seconds <= maximum_seconds):
        raise ValueError(f"{initial_seconds=} exceeds {maximum_seconds=}")


class DeliveryWorker:
    """One serial API owner per binding; other providers have their own worker."""

    def __init__(
        self,
        inbox: DeliveryInbox,
        endpoint: DeliveryEndpoint,
        api: ProviderAPI,
        *,
        retry_initial_seconds: float,
        retry_max_seconds: float,
        on_notice: Callable[[BaseException | None, dict[str, object]], None],
    ) -> None:
        """Configure publication for one binding without starting its worker.

        The API origin must match the endpoint. Retry delays are finite positive
        seconds, with the initial delay no greater than the maximum. on_notice
        is called synchronously with a failure or None and the publication facts.
        """

        if api.origin != endpoint.api_origin:
            raise ValueError(f"{api.origin=} should match {endpoint.api_origin=}")

        require_delivery_retry_settings(retry_initial_seconds, retry_max_seconds)

        (self.inbox, self.endpoint, self.api) = (inbox, endpoint, api)
        (self.retry_initial_seconds, self.retry_max_seconds) = (retry_initial_seconds, retry_max_seconds)
        self.on_notice = on_notice

        (self.changed, self.stopping, self.policy_changed) = (asyncio.Event(), asyncio.Event(), asyncio.Event())
        (self.paused, self._running) = (False, False)
        self.failure: DeliveryWorkerFailed | None = None
        self._api_receipt_received = False

    def wake(self) -> None:
        """Prompt the worker to check for pending items without bypassing backoff."""
        self.changed.set()

    def request_stop(self) -> None:
        """Wake the worker to stop without cancelling its current API or storage call.

        run() retires the API before it exits; unpublished requests remain queued.
        """
        self.stopping.set()
        self.changed.set()

    def status(self) -> dict[str, str]:
        """Local publisher state, independent of inbox or remote API readiness."""

        if self.failure is not None:
            return {"state": "stopped", "message": str(self.failure)}

        if self.paused:
            return {"state": "paused", "message": "This binding could not obtain a verified receipt or permission to retry. Inspect its operator diagnostics, resolve the reported cause, then restart delivery."}

        if self.stopping.is_set():
            return {"state": "stopping", "message": "Delivery is settling its original work; pending bytes remain retained."}

        if self._running:
            return {"state": "running", "message": "The local publisher is running; this is not a remote API availability check."}

        return {"state": "starting", "message": "The local publisher has not started yet."}

    def _facts(self, event: str, item: DeliveryOutcome) -> dict[str, str]:
        """Identify the retained request in a publisher notice."""

        return {"event": event, **item.acceptance()}

    async def _backoff(self, delay: float, minimum: float, seconds: float) -> None:
        """Wait until the retry deadline or shutdown, applying changed retry policy.

        A policy change recalculates jitter from delay and adds the server's
        minimum wait. The deadline stays anchored to this wait's original start;
        newly queued items do not shorten it.
        """
        loop = asyncio.get_running_loop()
        (started, deadline) = (loop.time(), loop.time() + seconds)

        while not self.stopping.is_set():
            remaining = deadline - loop.time()

            if remaining <= 0:
                return

            (stop, changed) = (asyncio.create_task(self.stopping.wait()), asyncio.create_task(self.policy_changed.wait()))

            try:
                await asyncio.wait((stop, changed), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.cancel()
                changed.cancel()
                await asyncio.gather(stop, changed, return_exceptions=True)

            if self.policy_changed.is_set():
                self.policy_changed.clear()
                delay = min(max(delay, self.retry_initial_seconds), self.retry_max_seconds)
                deadline = started + random.uniform(delay / 2, delay) + minimum

    async def _deliver(self, item: DeliveryOutcome) -> None:
        """Publish one retained request using only API-authorized exact retries.

        A verified receipt permits local queue removal. An item-specific refusal
        holds the request; a binding-wide refusal pauses this worker. Shutdown
        leaves unpublished work queued, and a stall notice does not cancel it.
        """
        loop = asyncio.get_running_loop()
        publication_started = loop.time()
        retried = alerted = False

        def facts(event: str) -> dict[str, object]:
            """Measure this request's publication wait in seconds, excluding queue residence.

            Another request's progress does not settle this retained report.
            """

            return {
                **self._facts(event, item),
                "publication_wait_seconds": loop.time() - publication_started
            }

        # One observation budget uses the existing captured API/retry grants.
        # The timer observes stalled storage too; it never cancels that write,
        # resends an accepted report, or renews when another retry starts.
        alert_seconds = self.api.exchange_seconds + self.api.retirement_seconds + self.retry_max_seconds

        def stalled() -> None:
            """Report stalled publication without cancelling its API or storage work."""
            nonlocal alerted
            alerted = True

            self.on_notice(
                TimeoutError("Publication is still pending; the publisher retains this report's original API and storage operations."),
                {
                    **facts("delivery_stalled"),
                    "api_receipt_received": self._api_receipt_received,
                    "alert_after_seconds": alert_seconds
                },
            )

        alarm = loop.call_later(alert_seconds, stalled)

        try:
            delay = self.retry_initial_seconds

            while not self.stopping.is_set():
                self._api_receipt_received = False

                try:
                    publish = self.api.complete_attempt if item.operation is TerminalOperation.COMPLETE else self.api.fail_attempt
                    receipt = await publish(item.body, execution_attempt_ref=item.execution_attempt_ref, owner=self)
                except ApiFailure as failure:
                    if not self.api.retirement_confirmed:
                        await self.api.retire(owner=self)

                    if self.api.can_replay_terminal(failure):
                        retried = True
                        delay = min(max(delay, self.retry_initial_seconds), self.retry_max_seconds)
                        seconds = random.uniform(delay / 2, delay)
                        minimum = failure.retry_after_seconds

                        if minimum is not None:
                            seconds += minimum

                        self.on_notice(failure, {**facts("delivery_retry"), "retry_seconds": seconds})

                        # New arrivals must not bypass backoff. Only shutdown ends
                        # this wait early; no overall expiry discards a durable item.
                        # silent-defaults: allow-contract -- No Retry-After header adds no server-directed delay.
                        await self._backoff(delay, 0 if minimum is None else minimum, seconds)

                        delay = min(self.retry_max_seconds, delay * 2)

                        continue

                    # A verified item conflict or receipt-shape mismatch belongs
                    # to this row. Trust, routing, topology and unknown refusals
                    # pause the binding, rather than holding every pending item.
                    item_refusal = (
                        (failure.status == 409 and failure.problem_code is not None) or
                        (failure.status == 200 and type(failure.cause) is ResponseRejected)
                    )

                    if not item_refusal:
                        self.paused = True

                        self.on_notice(failure, facts("delivery_binding_paused"))

                        return

                    await self.inbox.storage(
                        self.inbox.queue.hold,
                        item,
                        "The API did not return a matching publication receipt and exact replay is not authorized. Reconcile this retained request; never rerun science to replace it.",
                    )

                    self.on_notice(failure, facts("delivery_held"))

                    return
                else:
                    self._api_receipt_received = True

                    await self.inbox.storage(self.inbox.queue.remove_published, item)

                    self.on_notice(
                        None,
                        {
                            **facts("delivery_published"),
                            "replayed": receipt.replayed,
                            "recovered": retried or alerted
                        }
                    )

                    return
        finally:
            alarm.cancel()

    async def run(self) -> None:
        """Drain this binding serially and attempt API retirement before exiting.

        Idle workers wait for wake() or request_stop(). Publication and retirement
        failures are retained together in DeliveryWorkerFailed and reported through
        on_notice. Cancellation is re-raised after retirement with that failure as
        its cause, so cleanup diagnostics survive the cancelled caller.
        """

        if self._running:
            raise ValueError("A delivery binding already has its serial API worker.")

        self._running = True

        operation_failure: BaseException | None = None
        cleanup_failures: tuple[BaseException, ...] = ()

        try:
            while not self.stopping.is_set() and not self.paused:
                self._api_receipt_received = False

                self.changed.clear()

                pending_outcome = await self.inbox.storage(
                    self.inbox.queue.next_pending,
                    self.endpoint.api_origin,
                    self.endpoint.provider_ref
                )

                if pending_outcome is None:
                    await self.changed.wait()
                else:
                    await self._deliver(pending_outcome)
        except BaseException as cause:
            operation_failure = cause
        finally:
            try:
                await self.api.retire(owner=self)
            except BaseException as cause:
                cleanup_failures = (cause,)

            self._running = False

        if operation_failure is not None or cleanup_failures:
            self.failure = DeliveryWorkerFailed(
                cause=operation_failure,
                api_failure=self.api.last_failure,
                cleanup_failures=cleanup_failures,
                api_receipt_received=self._api_receipt_received,
            )

            self.on_notice(
                self.failure,
                {
                    "event": "delivery_worker_failed",
                    "api_origin": self.endpoint.api_origin,
                    "provider_ref": self.endpoint.provider_ref,
                }
            )

            if isinstance(operation_failure, asyncio.CancelledError):
                raise operation_failure from self.failure

            for cause in cleanup_failures:
                if isinstance(cause, asyncio.CancelledError):
                    raise cause from self.failure

            raise self.failure from None
