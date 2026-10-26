"""Receive bounded final outcomes through provider-scoped local HTTP sockets.

The queue commit is the handoff boundary. This component has no API client,
scientific worker, publication policy or filesystem export operation.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import fcntl
import json
import os
import socket
import stat
from contextlib import ExitStack, contextmanager

import h11

from provider_core.attempt_record import TerminalOperation
from provider_core.attempt_response import require_attempt_ref
from provider_core.delivery_client import DeliveryUnavailable
from provider_core.delivery_store import DeliveryConflict, DeliveryFull, DeliveryInventoryUnavailable, DeliveryOutcome, DeliveryStorageFailed

# The existing terminal request codec owns this complete-body wire ceiling.
# repo-check: allow-module-constant
MAX_REQUEST_BYTES = 2097152  # Existing read_complete_body wire ceiling; fail remains parser-bounded to 16 KiB.
# Bound IPC HTTP framing before body admission.
# repo-check: allow-module-constant
MAX_HEADER_BYTES = 16384
# Bound concurrently retained local listener tasks.
# repo-check: allow-module-constant
MAX_CONNECTIONS = 32


class DeliveryRequestRejected(ValueError):
    """Code-owned IPC refusal safe to report without request bytes or headers."""


@contextmanager
def _socket_grant(path, socket_group_id):
    """Hold one private directory lock before replacing its retired listener.

    Ports tools/provider_proxy.py::socket_grant at NMRNet 8e14575. This new
    listener needs no separate readiness file; its owner closes servers first.
    """
    shared = socket_group_id is not None
    # silent-defaults: allow-contract -- Directory access mode follows the explicit same-UID or shared-group socket grant.
    parent_modes = (0o710, 0o2710) if shared else (0o700,)
    # silent-defaults: allow-contract -- Socket mode follows the explicit same-UID or shared-group connection grant.
    socket_mode = 0o660 if shared else 0o600
    if shared and socket_group_id != os.getegid():
        raise DeliveryUnavailable("Cannot open delivery IPC: the configured socket group must be the daemon's primary GID.")
    parent = path.parent.lstat()
    if (path.parent.resolve() != path.parent or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) not in parent_modes
            or (shared and parent.st_gid != socket_group_id)):
        # silent-defaults: allow-contract -- Parent permission requirements follow the explicit shared-group or same-UID socket grant.
        required_parent_mode = "mode 0710 or 02710 with the configured socket GID; " if shared else "mode 0700; "
        raise DeliveryUnavailable(
            "Cannot open delivery IPC: require a canonical daemon-owned parent, "
            + required_parent_mode
            + f"observed uid {parent.st_uid}, gid {parent.st_gid}, mode {stat.S_IMODE(parent.st_mode):04o}, caller uid {os.geteuid()}."
        )
    descriptor = os.open(path.parent / ".socket.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    listener = None
    try:
        lock = os.fstat(descriptor)
        if not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.geteuid() or stat.S_IMODE(lock.st_mode) != 0o600:
            raise DeliveryUnavailable("Cannot open delivery IPC: its lock must be a caller-owned mode 0600 regular file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            old = path.lstat()
        except FileNotFoundError:
            old = None
        if old is not None:
            if (not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.geteuid() or stat.S_IMODE(old.st_mode) != socket_mode
                    or (shared and old.st_gid != socket_group_id)):
                raise DeliveryUnavailable("Cannot replace delivery IPC: the prior leaf is not its private socket. Leave it unchanged for inspection.")
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Grant group connect access at birth; credentials and database stay private.
        # silent-defaults: allow-contract -- Birth umask enforces the selected private or shared-group socket posture.
        previous = os.umask(0o117 if shared else 0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(previous)
        identity = path.lstat()
        listener.listen(MAX_CONNECTIONS)
        listener.setblocking(False)
        yield listener
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise DeliveryUnavailable("Delivery IPC changed while owned; its replacement was left untouched.")
        path.unlink()
    finally:
        if listener is not None:
            listener.close()
        os.close(descriptor)


class DeliveryInbox:
    """Own listeners, client handlers and original offloaded SQLite operations."""

    def __init__(self, queue, endpoints, *, request_seconds, on_accepted, on_failure, binding_status):
        self.queue, self.endpoints = queue, endpoints
        self.request_seconds = request_seconds
        self.on_accepted, self.on_failure = on_accepted, on_failure
        self.binding_status = binding_status
        self._grants, self._servers = ExitStack(), []
        self._clients, self._storage_tasks = set(), set()
        self.failed, self.failure = asyncio.Event(), None

    async def start(self):
        try:
            for endpoint in self.endpoints:
                try:
                    listener = self._grants.enter_context(_socket_grant(endpoint.socket_path, endpoint.socket_group_id))
                    server = await asyncio.start_unix_server(
                        lambda reader, writer, selected=endpoint: self._client(selected, reader, writer),
                        sock=listener, limit=MAX_HEADER_BYTES,
                    )
                except BaseException as cause:
                    self.on_failure(cause, {
                        "event": "delivery_binding_start_failed", "api_origin": endpoint.api_origin,
                        "provider_ref": endpoint.provider_ref, "socket_path": str(endpoint.socket_path),
                    })
                    raise
                self._servers.append(server)
        except BaseException as operation:
            try:
                await self.close()
            except BaseException as cleanup:
                if isinstance(operation, asyncio.CancelledError):
                    raise operation from cleanup
                raise BaseExceptionGroup("Delivery listener startup and independent cleanup failed.", [operation, cleanup]) from None
            raise

    def _storage_done(self, task):
        self._storage_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not isinstance(error, (DeliveryFull, DeliveryConflict, DeliveryInventoryUnavailable)):
            self.failure = error
            self.failed.set()
            self.on_failure(error, {"event": "delivery_storage_failed"})

    async def storage(self, operation, *arguments):
        # Cancelling an HTTP waiter cannot close SQLite or discard a write
        # that may still commit. close() joins these original tasks first.
        task = asyncio.create_task(asyncio.to_thread(operation, *arguments))
        self._storage_tasks.add(task)
        task.add_done_callback(self._storage_done)
        return await asyncio.shield(task)

    async def _read(self, reader, connection):
        request, body = None, bytearray()
        async with asyncio.timeout(self.request_seconds):
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    connection.receive_data(await reader.read(65536))
                elif isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.Data):
                    if len(body) + len(event.data) > MAX_REQUEST_BYTES:
                        raise DeliveryRequestRejected("Delivery request exceeds the terminal body limit.")
                    body.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    return request, bytes(body)
                elif isinstance(event, h11.ConnectionClosed):
                    raise ConnectionError("Delivery client disconnected before its complete request.")

    def _headers(self, request, endpoint):
        values = {}
        for name, value in request.headers:
            if name in (b"nmr-api-origin", b"nmr-provider-ref", b"nmr-job-ref"):
                if name in values:
                    raise DeliveryRequestRejected("Delivery identity headers must not repeat.")
                try:
                    values[name] = value.decode("ascii")
                except UnicodeDecodeError:
                    raise DeliveryRequestRejected("Delivery identity headers must use ASCII text.") from None
        if (values.get(b"nmr-api-origin"), values.get(b"nmr-provider-ref")) != (endpoint.api_origin, endpoint.provider_ref):
            raise DeliveryRequestRejected("This socket is bound to another API/provider; correct the deployment grant.")
        return values

    async def _dispatch(self, endpoint, request, body):
        headers = self._headers(request, endpoint)
        try:
            parts = request.target.decode("ascii").split("/")
        except UnicodeDecodeError:
            raise DeliveryRequestRejected("Delivery operation must use an ASCII route.") from None
        if request.method == b"POST" and len(parts) == 4 and parts[1] == "submit":
            try:
                item = DeliveryOutcome(
                    endpoint.api_origin, endpoint.provider_ref, headers.get(b"nmr-job-ref"), parts[3],
                    TerminalOperation(parts[2]), body,
                )
            except ValueError:
                raise DeliveryRequestRejected("Delivery refused an invalid terminal operation, identity or request body; correct the producer request.") from None
            accepted = await self.storage(self.queue.enqueue, item)
            self.on_accepted(endpoint)
            return accepted
        if request.method != b"GET" or body:
            raise DeliveryRequestRejected("Delivery supports only outcome submission and read-only status.")
        if parts == ["", "pending"]:
            rows = await self.storage(self.queue.ipc_inventory, endpoint.api_origin, endpoint.provider_ref)
            return {
                "api_origin": endpoint.api_origin, "provider_ref": endpoint.provider_ref, "items": rows,
                "capacity": {"provider_items": self.queue.limits.provider_items, "provider_bytes": self.queue.limits.provider_bytes},
                "publisher": self.binding_status(endpoint),
            }
        if len(parts) == 3 and parts[1] == "status":
            try:
                require_attempt_ref(parts[2])
            except ValueError:
                raise DeliveryRequestRejected("Delivery status requires a valid Attempt reference.") from None
            return await self.storage(self.queue.status, endpoint.api_origin, endpoint.provider_ref, parts[2])
        raise DeliveryRequestRejected("Unsupported delivery operation.")

    async def _respond(self, connection, writer, status, document):
        content = json.dumps(document, allow_nan=False, separators=(",", ":")).encode("utf-8")
        writer.write(connection.send(h11.Response(status_code=status, headers=[
            (b"Content-Type", b"application/json"), (b"Content-Length", str(len(content)).encode()), (b"Connection", b"close")
        ])))
        writer.write(connection.send(h11.Data(data=content)))
        writer.write(connection.send(h11.EndOfMessage()))
        async with asyncio.timeout(self.request_seconds):
            await writer.drain()

    async def _client(self, endpoint, reader, writer):
        task = asyncio.current_task()
        self._clients.add(task)
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEADER_BYTES)
        durable_enqueue_confirmed = False
        try:
            if len(self._clients) > MAX_CONNECTIONS:
                return
            request, body = await self._read(reader, connection)
            try:
                accepted = await self._dispatch(endpoint, request, body)
                durable_enqueue_confirmed = request.method == b"POST"
                status, reply = 200, accepted
            except DeliveryFull:
                status, reply = 429, {"error": "Delivery capacity is full; no new item was accepted."}
            except DeliveryConflict:
                status, reply = 409, {"error": "The original outstanding terminal request remains unchanged."}
            except DeliveryStorageFailed:
                status, reply = 503, {"error": "Delivery storage is unavailable; acceptance is not confirmed."}
            except DeliveryInventoryUnavailable as cause:
                self.on_failure(cause, {"event": "delivery_inventory_unavailable", "provider_ref": endpoint.provider_ref})
                status, reply = 503, {"error": "Pending inventory is oversized; publication continues and per-Attempt status remains available."}
            except DeliveryRequestRejected as cause:
                self.on_failure(cause, {"event": "delivery_request_rejected", "provider_ref": endpoint.provider_ref})
                status, reply = 400, {"error": "The request does not match this provider's delivery protocol."}
            await self._respond(connection, writer, status, reply)
        except (OSError, ConnectionError, TimeoutError, h11.ProtocolError) as cause:
            self.on_failure(cause, {
                "event": "delivery_ipc_failed", "provider_ref": endpoint.provider_ref,
                "durable_enqueue_confirmed": durable_enqueue_confirmed,
            })
        except DeliveryRequestRejected as cause:
            self.on_failure(cause, {"event": "delivery_request_rejected", "provider_ref": endpoint.provider_ref})
            try:
                await self._respond(connection, writer, 400, {"error": "The delivery request exceeds its supported protocol limits."})
            except (OSError, TimeoutError, h11.ProtocolError) as response_failure:
                self.on_failure(response_failure, {"event": "delivery_refusal_reply_failed", "provider_ref": endpoint.provider_ref})
        finally:
            writer.close()
            try:
                async with asyncio.timeout(self.request_seconds):
                    await writer.wait_closed()
            except (OSError, TimeoutError) as cause:
                writer.transport.abort()
                self.on_failure(cause, {"event": "delivery_ipc_close_failed", "provider_ref": endpoint.provider_ref})
            self._clients.discard(task)

    async def stop_accepting(self):
        """Stop new clients without interrupting accepted requests or storage."""
        for server in self._servers:
            server.close()
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()

    async def close(self):
        await self.stop_accepting()
        failures = []
        for tasks in (self._clients, self._storage_tasks):
            if tasks:
                settled = await asyncio.gather(*(asyncio.shield(task) for task in tuple(tasks)), return_exceptions=True)
                failures.extend(value for value in settled if isinstance(value, BaseException))
        self._grants.close()
        if failures:
            raise BaseExceptionGroup("Delivery IPC retirement failed after all original operations settled.", failures)
