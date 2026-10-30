"""Own the shared push-only intake and publishers derived from retained rows."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
import time

from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_service import DeliveryService
from provider_core.delivery_store import DeliveryQueue
from provider_core.delivery_worker import require_delivery_retry_settings
from provider_core.shared_delivery_ipc import SharedDeliveryInbox
from provider_core.shared_delivery_worker import SharedDeliveryWorker
from provider_core.process_status import ProcessStatus


class SharedDeliveryService(DeliveryService):
    # silent-defaults: allow-contract -- A fresh queue has no legacy publication inputs to associate.
    def __init__(self, directory, *, limits, socket, proxy_socket, request_seconds, retry_initial_seconds,
                 retry_max_seconds, on_notice, diagnostic_status, legacy_material=(), live_configuration=None,
                 status_directory=None):
        if not (type(request_seconds) in (int, float) and math.isfinite(request_seconds) and request_seconds > 0):
            raise ValueError("Delivery IPC request deadline must be finite positive seconds.")
        require_delivery_retry_settings(retry_initial_seconds, retry_max_seconds)
        self.directory, self.limits, self.socket, self.proxy_socket = directory, limits, socket, proxy_socket
        self.request_seconds = request_seconds
        self.retry_initial_seconds, self.retry_max_seconds = retry_initial_seconds, retry_max_seconds
        self.on_notice, self.diagnostic_status, self.legacy_material = on_notice, diagnostic_status, tuple(legacy_material)
        self.live_configuration, self.next_reload = live_configuration, 0.0
        self.status_directory, self.process_status = status_directory, None
        self.ready, self.stopping, self.changed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.queue, self.inbox = None, None
        self.workers, self._worker_tasks, self._manager_task = {}, {}, None
        self.worker_failures = ()
        self._close_task, self._started, self._startup_task = None, False, None
        self.retirement_confirmed = False

    def request_stop(self):
        super().request_stop()
        self.changed.set()

    def _accepted(self, outcome):
        worker = self.workers.get(outcome.key[:2])
        if worker is not None:
            worker.wake()
        self.changed.set()

    async def _start(self):
        if self.live_configuration is not None:
            self._apply_settings(self.live_configuration.read())
        self.queue = DeliveryQueue(self.directory, limits=self.limits)
        for material in self.legacy_material:
            self.queue.associate_legacy_material(material)
        held = self.queue.hold_legacy_rows()
        if held:
            self.on_notice(None, {"event": "delivery_legacy_rows_held", "count": held})
        self.inbox = SharedDeliveryInbox(self.queue, self.socket, request_seconds=self.request_seconds,
                                         on_accepted=self._accepted, on_failure=self.on_notice)
        await self.inbox.start()
        if self.stopping.is_set():
            return
        if self.status_directory is not None:
            self.process_status = ProcessStatus(self.status_directory, "delivery")
            self.process_status.refresh("ready")
        self._manager_task = asyncio.create_task(self._manage())
        self._manager_task.add_done_callback(self._manager_done)
        self.on_notice(None, {"event": "delivery_ready"})
        self.ready.set()

    def _apply_settings(self, settings):
        self.limits = settings.limits
        self.retry_initial_seconds, self.retry_max_seconds = settings.retry_initial_seconds, settings.retry_max_seconds
        if self.queue is not None:
            self.queue.limits = settings.limits
        for worker in self.workers.values():
            changed = (worker.retry_initial_seconds, worker.retry_max_seconds) != (settings.retry_initial_seconds, settings.retry_max_seconds)
            worker.retry_initial_seconds, worker.retry_max_seconds = settings.retry_initial_seconds, settings.retry_max_seconds
            if changed:
                worker.policy_changed.set()
        self.on_notice.set_level(settings.log_level)

    def _reload(self):
        if self.live_configuration is None or time.monotonic() < self.next_reload:
            return
        self.next_reload = time.monotonic() + 5
        try:
            settings = self.live_configuration.read()
        except (OSError, ValueError) as error:
            self.on_notice(error, {"event": "delivery_reload_failed", "failure_type": type(error).__name__})
            return
        self._apply_settings(settings)

    def _manager_done(self, task):
        if task.cancelled():
            self.inbox.failure = RuntimeError("The delivery manager was cancelled; preserve pending work and restart the service.")
            self.inbox.failed.set()
            return
        error = task.exception()
        if error is not None:
            self.inbox.failure = error
            self.inbox.failed.set()
            self.on_notice(error, {"event": "delivery_manager_failed"})

    def _worker_done(self, key, task):
        if task.cancelled():
            self.inbox.failure = RuntimeError("A delivery worker was cancelled; preserve pending work and settle its API owner.")
            self.inbox.failed.set()
            return
        error = task.exception()
        if error is not None:
            self.inbox.failure = error
            self.inbox.failed.set()
        elif not self.workers[key].paused:
            del self.workers[key]
            del self._worker_tasks[key]
            self.changed.set()

    async def _manage(self):
        while not self.stopping.is_set():
            if self.process_status is not None:
                self.process_status.refresh("ready")
            self._reload()
            self.changed.clear()
            bindings = await self.inbox.storage(self.queue.pending_bindings)
            for origin, provider in bindings:
                key = (origin, provider)
                if key in self.workers:
                    self.workers[key].wake()
                    continue
                endpoint = DeliveryEndpoint(origin, provider, self.socket.socket_path, self.socket.socket_group_id)
                worker = SharedDeliveryWorker(self.inbox, endpoint, self.proxy_socket,
                    retry_initial_seconds=self.retry_initial_seconds, retry_max_seconds=self.retry_max_seconds,
                    on_notice=self.on_notice)
                self.workers[key] = worker
                task = asyncio.create_task(worker.run())
                self._worker_tasks[key] = task
                task.add_done_callback(lambda completed, selected=key: self._worker_done(selected, completed))
            try:
                async with asyncio.timeout(5):
                    await self.changed.wait()
            # silent-defaults: allow-contract -- The bounded poll finds commits whose submitter disconnected before waking the manager.
            except TimeoutError:
                # A producer can disconnect after commit before its wake callback.
                pass

    async def _shutdown(self):
        failures = []
        if self._startup_task is not None:
            await asyncio.gather(asyncio.shield(self._startup_task), return_exceptions=True)
        if self.inbox is not None:
            try:
                await self.inbox.stop_accepting()
            except BaseException as cause:
                failures.append(cause)
        if self._manager_task is not None:
            await asyncio.gather(asyncio.shield(self._manager_task), return_exceptions=True)
        if self.process_status is not None:
            try:
                self.process_status.refresh("stopping")
            except OSError as cause:
                failures.append(cause)
        for worker in tuple(self.workers.values()):
            worker.request_stop()
        settled = await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._worker_tasks.values())), return_exceptions=True)
        # repo-check: allow-tuple-generator -- Retain the final independent worker failures as immutable shutdown evidence.
        self.worker_failures = tuple(value for value in settled if isinstance(value, BaseException))
        for worker in tuple(self.workers.values()):
            if worker.api is not None and not worker.api.retirement_confirmed:
                try:
                    await worker.api.retire(owner=worker)
                except BaseException as cause:
                    failures.append(cause)
        if self.inbox is not None:
            try:
                await self.inbox.close()
            except BaseException as cause:
                failures.append(cause)
        if failures:
            raise BaseExceptionGroup("Delivery shutdown could not settle all original resources; the database owner remains held.", failures)
        if self.queue is not None:
            self.queue.close()
        self.retirement_confirmed = True
