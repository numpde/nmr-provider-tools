"""Publish each retained handoff using its own original API authority."""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import replace

from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings_from_material
from provider_core.delivery_worker import DeliveryWorker, DeliveryWorkerFailed, require_delivery_retry_settings


def publication_api(material, proxy_socket):
    settings, _ = api_settings_from_material(material.api_fields(), credential_bytes=material.credential_bytes,
                                             ca_bytes=material.ca_bytes)
    http = replace(settings.http, proxy_socket=str(proxy_socket))
    return ProviderAPI(settings.identity, http.new_transport(), expected_topology=settings.expected_topology)


class SharedDeliveryWorker(DeliveryWorker):
    """Serialize one provider's rows without retaining a provider configuration."""

    def __init__(self, inbox, endpoint, proxy_socket, *, retry_initial_seconds, retry_max_seconds, on_notice):
        require_delivery_retry_settings(retry_initial_seconds, retry_max_seconds)
        self.inbox, self.endpoint, self.proxy_socket = inbox, endpoint, proxy_socket
        self.retry_initial_seconds, self.retry_max_seconds = retry_initial_seconds, retry_max_seconds
        self.on_notice = on_notice
        self.changed, self.stopping, self.policy_changed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.paused, self._running = False, False
        self.failure, self._api_receipt_received, self.api = None, False, None

    async def run(self):
        if self._running:
            raise ValueError("A delivery provider already has its serial worker.")
        self._running = True
        operation_failure, cleanup_failures = None, ()
        try:
            while not self.stopping.is_set() and not self.paused:
                self._api_receipt_received = False
                self.changed.clear()
                handoff = await self.inbox.storage(
                    self.inbox.queue.next_pending_handoff, self.endpoint.api_origin, self.endpoint.provider_ref
                )
                if handoff is None:
                    return
                self.api = publication_api(handoff.publication, self.proxy_socket)
                await self._deliver(handoff.outcome)
                await self.api.retire(owner=self)
                self.api = None
        except BaseException as cause:
            operation_failure = cause
        finally:
            if self.api is not None and not self.api.retirement_confirmed:
                try:
                    await self.api.retire(owner=self)
                except BaseException as cause:
                    cleanup_failures = (cause,)
            self._running = False
        if operation_failure is not None or cleanup_failures:
            # silent-defaults: allow-contract -- No API failure exists if constructing the first API owner failed.
            self.failure = DeliveryWorkerFailed(
                cause=operation_failure, api_failure=None if self.api is None else self.api.last_failure,
                cleanup_failures=cleanup_failures, api_receipt_received=self._api_receipt_received,
            )
            self.on_notice(self.failure, {"event": "delivery_worker_failed", "api_origin": self.endpoint.api_origin,
                                          "provider_ref": self.endpoint.provider_ref})
            if isinstance(operation_failure, asyncio.CancelledError):
                raise operation_failure from self.failure
            for cause in cleanup_failures:
                if isinstance(cause, asyncio.CancelledError):
                    raise cause from self.failure
            raise self.failure from None
