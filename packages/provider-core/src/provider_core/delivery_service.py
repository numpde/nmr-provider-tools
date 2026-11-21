"""Own one host queue, scoped listeners and independently retired API workers.

The deployment supplies admitted bindings. This service has no Docker socket,
scientific callback, producer result store or ability to choose another origin.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import dataclass

from provider_core.api import ProviderAPI
from provider_core.delivery_client import DeliveryEndpoint, DeliveryUnavailable
from provider_core.delivery_ipc import DeliveryInbox
from provider_core.delivery_store import MAX_BINDINGS, DeliveryQueue
from provider_core.delivery_worker import DeliveryWorker, require_delivery_retry_settings
from provider_core.tools import require_positive_number

@dataclass(frozen=True, slots=True)
class DeliveryBinding:
    endpoint: DeliveryEndpoint
    api: ProviderAPI


class DeliveryService:
    """Keep original shutdown work reachable when a caller stops waiting."""

    def __init__(self, directory, *, limits, bindings, request_seconds, retry_initial_seconds, retry_max_seconds, on_notice, diagnostic_status):
        bindings = tuple(bindings)
        binding_count = len(bindings)
        min_bindings = 1

        if not (min_bindings <= binding_count <= MAX_BINDINGS):
            raise ValueError(f"{binding_count=} should be between {min_bindings=} and {MAX_BINDINGS=}")

        binding_keys = {(binding.endpoint.api_origin, binding.endpoint.provider_ref) for binding in bindings}
        socket_paths = {binding.endpoint.socket_path for binding in bindings}
        socket_directories = {binding.endpoint.socket_path.parent for binding in bindings}

        if not (len(binding_keys) == binding_count):
            raise ValueError(f"{len(binding_keys)=} should match {binding_count=}")

        if not (len(socket_paths) == binding_count):
            raise ValueError(f"{len(socket_paths)=} should match {binding_count=}")

        if not (len(socket_directories) == binding_count):
            raise ValueError(f"{len(socket_directories)=} should match {binding_count=}")

        api_owner_count = len({id(binding.api) for binding in bindings})

        if not (api_owner_count == binding_count):
            raise ValueError(f"{api_owner_count=} should match {binding_count=}")

        required_total_items = binding_count * limits.provider_items

        if not (required_total_items <= limits.total_items):
            raise ValueError(f"{limits.total_items=} should cover {required_total_items=}")

        required_total_bytes = binding_count * limits.provider_bytes

        if not (required_total_bytes <= limits.total_bytes):
            raise ValueError(f"{limits.total_bytes=} should cover {required_total_bytes=}")

        require_positive_number(request_seconds)

        require_delivery_retry_settings(retry_initial_seconds, retry_max_seconds)
        (self.directory, self.limits, self.bindings) = (directory, limits, bindings)
        self.request_seconds = request_seconds

        (self.retry_initial_seconds, self.retry_max_seconds) = (retry_initial_seconds, retry_max_seconds)
        self.on_notice = on_notice
        self.diagnostic_status = diagnostic_status

        (self.ready, self.stopping) = (asyncio.Event(), asyncio.Event())
        (self.queue, self.inbox) = (None, None)
        (self.workers, self._worker_tasks) = ({}, ())
        self.worker_failures = ()

        (self._close_task, self._started) = (None, False)
        self._startup_task = None
        self.retirement_confirmed = False

    def request_stop(self):
        self.stopping.set()

        for worker in self.workers.values():
            worker.request_stop()

    def _accepted(self, endpoint):
        self.workers[(endpoint.api_origin, endpoint.provider_ref)].wake()

    def _binding_status(self, endpoint):
        return {**self.workers[(endpoint.api_origin, endpoint.provider_ref)].status(), "diagnostics": self.diagnostic_status()}

    @staticmethod
    def _observed(task):
        # Original tasks remain retained for close(); consume their result so
        # an interrupted waiter never becomes an unhandled-task traceback.
        if not task.cancelled():
            task.exception()

    async def _start(self):
        self.queue = DeliveryQueue(self.directory, limits=self.limits)

        configured_bindings = {(binding.endpoint.api_origin, binding.endpoint.provider_ref) for binding in self.bindings}
        missing_bindings = set(self.queue.outstanding_bindings()) - configured_bindings

        if missing_bindings:
            failure = DeliveryUnavailable(f"{missing_bindings=} should be configured; retained requests remain unchanged")

            for (origin, provider) in sorted(missing_bindings):
                self.on_notice(failure, {"event": "delivery_binding_missing", "api_origin": origin, "provider_ref": provider})

            raise failure

        self.inbox = DeliveryInbox(
            self.queue,
            # repo-check: allow-tuple-generator -- The inbox retains its complete endpoint collection for its lifetime.
            tuple(binding.endpoint for binding in self.bindings),
            request_seconds=self.request_seconds,
            on_accepted=self._accepted,
            on_failure=self.on_notice,
            binding_status=self._binding_status,
        )

        for binding in self.bindings:
            worker = DeliveryWorker(
                self.inbox,
                binding.endpoint,
                binding.api,
                retry_initial_seconds=self.retry_initial_seconds,
                retry_max_seconds=self.retry_max_seconds,
                on_notice=self.on_notice,
            )

            self.workers[(binding.endpoint.api_origin, binding.endpoint.provider_ref)] = worker

        await self.inbox.start()

        if self.stopping.is_set():
            return

        # repo-check: allow-tuple-generator -- Retain original worker tasks across repeated or canceled shutdown waits.
        self._worker_tasks = tuple(asyncio.create_task(worker.run()) for worker in self.workers.values())

        for task in self._worker_tasks:
            task.add_done_callback(self._observed)

        self.on_notice(None, {"event": "delivery_ready", "binding_count": len(self.bindings)})
        self.ready.set()

    async def _wait(self):
        watchers = (asyncio.create_task(self.stopping.wait()), asyncio.create_task(self.inbox.failed.wait()))

        try:
            await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)

            if self.inbox.failed.is_set():
                raise self.inbox.failure
        finally:
            for task in watchers:
                task.cancel()

            await asyncio.gather(*watchers, return_exceptions=True)

    async def run(self):
        if self._started:
            raise ValueError(f"{self._started=} should be False")

        if self.stopping.is_set():
            raise ValueError(f"{self.stopping.is_set()=} should be False")

        self._started = True
        self._startup_task = asyncio.create_task(self._start())

        self._startup_task.add_done_callback(self._observed)

        try:
            await asyncio.shield(self._startup_task)
            await self._wait()
        except BaseException as operation:
            try:
                await self.close()
            except BaseException as cleanup:
                if isinstance(operation, asyncio.CancelledError):
                    raise operation from cleanup

                raise BaseExceptionGroup("Delivery service failed and shutdown remains unresolved.", [operation, cleanup]) from None

            raise
        else:
            await self.close()

    async def _shutdown(self):
        failures = []

        if self._startup_task is not None:
            # Startup may still be binding a socket. Join that original handoff
            # before closing its resources; stop prevents it launching workers.
            await asyncio.gather(asyncio.shield(self._startup_task), return_exceptions=True)

        if self.inbox is not None:
            try:
                await self.inbox.stop_accepting()
            except BaseException as cause:
                failures.append(cause)

        for worker in self.workers.values():
            worker.request_stop()

        worker_outcomes = await asyncio.gather(*(asyncio.shield(task) for task in self._worker_tasks), return_exceptions=True)

        # repo-check: allow-tuple-generator -- Settled worker failures are stable evidence distinct from later cleanup failures.
        self.worker_failures = tuple(worker_outcome for worker_outcome in worker_outcomes if isinstance(worker_outcome, BaseException))

        # A completed worker failure is evidence, not proof of retired I/O.
        # Later close() attempts settle these same API owners without sending.
        for binding in self.bindings:
            if not binding.api.retirement_confirmed:
                worker = self.workers.get((binding.endpoint.api_origin, binding.endpoint.provider_ref))

                try:
                    await binding.api.retire(owner=worker)
                except BaseException as cause:
                    failures.append(cause)

        if self.inbox is not None:
            try:
                await self.inbox.close()
            except BaseException as cause:
                failures.append(cause)

        # repo-check: allow-literal-guard-error -- Each child retains its original resource failure; the group identifies the shared shutdown boundary
        if failures:
            raise BaseExceptionGroup("Delivery shutdown could not settle all original resources; the database owner remains held.", failures)

        if self.queue is not None:
            self.queue.close()

        self.retirement_confirmed = True

    async def close(self):
        self.request_stop()

        if (self._close_task is None) or ((self._close_task.done()) and (self._close_task.exception() is not None)):
            self._close_task = asyncio.create_task(self._shutdown())

            self._close_task.add_done_callback(self._observed)

        await asyncio.shield(self._close_task)
