"""Drain one configured provider's immutable terminal requests independently.

ProviderAPI owns signing, matching receipts and exact-request replay permission.
This owner supplies pacing and pending-only storage; it never runs science or
manufactures producer Attempt records to reuse the old file-store publisher.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
import random

from provider_core.api import ApiFailure
from provider_core.attempt_record import TerminalOperation
from provider_core.response_json import ResponseRejected


class DeliveryWorkerFailed(RuntimeError):
    """Retain independent API/storage and retirement causes for the operator."""

    def __init__(self, *, cause, api_failure, cleanup_failures, api_receipt_received):
        super().__init__("Delivery stopped for this provider binding; preserve its pending queue and settle the retained failure before restarting delivery.")
        self.cause, self.api_failure = cause, api_failure
        self.cleanup_failures = cleanup_failures
        self.api_receipt_received = api_receipt_received


def require_delivery_retry_settings(initial_seconds, maximum_seconds):
    """Share admission before service startup and standalone worker construction."""
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
           for value in (initial_seconds, maximum_seconds)) or initial_seconds > maximum_seconds:
        raise ValueError("Delivery retry delays must be finite positive seconds, with initial delay no greater than maximum.")


class DeliveryWorker:
    """One serial API owner per binding; other providers have their own worker."""

    def __init__(self, inbox, endpoint, api, *, retry_initial_seconds, retry_max_seconds, on_notice):
        if api.origin != endpoint.api_origin:
            raise ValueError("Delivery signing origin differs from its configured binding.")
        require_delivery_retry_settings(retry_initial_seconds, retry_max_seconds)
        self.inbox, self.endpoint, self.api = inbox, endpoint, api
        self.retry_initial_seconds, self.retry_max_seconds = retry_initial_seconds, retry_max_seconds
        self.on_notice = on_notice
        self.changed, self.stopping = asyncio.Event(), asyncio.Event()
        self.paused, self._running = False, False
        self.failure, self._api_receipt_received = None, False

    def wake(self):
        self.changed.set()

    def request_stop(self):
        self.stopping.set()
        self.changed.set()

    def status(self):
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

    def _facts(self, event, item):
        return {"event": event, **item.acceptance()}

    async def _deliver(self, item):
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
                    delay = min(max(delay, self.retry_initial_seconds), self.retry_max_seconds)
                    seconds = random.uniform(delay / 2, delay)
                    minimum = failure.retry_after_seconds
                    if minimum is not None:
                        seconds += minimum
                    self.on_notice(failure, {**self._facts("delivery_retry", item), "retry_seconds": seconds})
                    # New arrivals must not bypass backoff. Only shutdown ends
                    # this wait early; no overall expiry discards a durable item.
                    try:
                        async with asyncio.timeout(seconds):
                            await self.stopping.wait()
                    # silent-defaults: allow-contract -- Backoff timer completion ends only this wait; it does not discard the pending outcome or grant another operation.
                    except TimeoutError:
                        pass
                    delay = min(self.retry_max_seconds, delay * 2)
                    continue
                # A verified item conflict or receipt-shape mismatch belongs
                # to this row. Trust, routing, topology and unknown refusals
                # pause the binding, rather than holding every pending item.
                item_refusal = (
                    failure.status == 409 and failure.problem_code is not None
                    or failure.status == 200 and type(failure.cause) is ResponseRejected
                )
                if not item_refusal:
                    self.paused = True
                    self.on_notice(failure, self._facts("delivery_binding_paused", item))
                    return
                await self.inbox.storage(
                    self.inbox.queue.hold, item,
                    "The API did not return a matching publication receipt and exact replay is not authorized. Reconcile this retained request; never rerun science to replace it.",
                )
                self.on_notice(failure, self._facts("delivery_held", item))
                return
            else:
                self._api_receipt_received = True
                await self.inbox.storage(self.inbox.queue.remove_published, item)
                self.on_notice(None, {**self._facts("delivery_published", item), "replayed": receipt.replayed})
                return

    async def run(self):
        if self._running:
            raise ValueError("A delivery binding already has its serial API worker.")
        self._running = True
        operation_failure, cleanup_failures = None, ()
        try:
            while not self.stopping.is_set() and not self.paused:
                self._api_receipt_received = False
                self.changed.clear()
                item = await self.inbox.storage(
                    self.inbox.queue.next_pending, self.endpoint.api_origin, self.endpoint.provider_ref
                )
                if item is None:
                    await self.changed.wait()
                else:
                    await self._deliver(item)
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
                cause=operation_failure, api_failure=self.api.last_failure,
                cleanup_failures=cleanup_failures, api_receipt_received=self._api_receipt_received,
            )
            self.on_notice(self.failure, {
                "event": "delivery_worker_failed", "api_origin": self.endpoint.api_origin,
                "provider_ref": self.endpoint.provider_ref,
            })
            if isinstance(operation_failure, asyncio.CancelledError):
                raise operation_failure from self.failure
            for cause in cleanup_failures:
                if isinstance(cause, asyncio.CancelledError):
                    raise cause from self.failure
            raise self.failure from None
