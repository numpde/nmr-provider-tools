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
import reprlib
import socket
import stat
from contextlib import ExitStack, contextmanager

import h11

from provider_core.attempt_record import TerminalOperation
from provider_core.validation.upstream.nmr.v1.attempt_request import MAX_COMPLETE_REQUEST_BYTES
from provider_core.validation.upstream.nmr.v1.attempt_response import require_attempt_ref
from provider_core.delivery_client import DeliveryUnavailable
from provider_core.delivery_store import DeliveryConflict, DeliveryFull, DeliveryInventoryUnavailable, DeliveryOutcome, DeliveryStorageFailed

# Bound IPC HTTP framing before body admission.
# repo-check: allow-module-constant
MAX_HEADER_BYTES = 16384

# Bound concurrently retained local listener tasks.
# repo-check: allow-module-constant
MAX_CONNECTIONS = 32


# Preserve ordinary identity fields while bounding malformed IPC metadata
# repo-check: allow-module-constant
_IPC_EVIDENCE = reprlib.Repr()
_IPC_EVIDENCE.maxstring = 256
_IPC_EVIDENCE.maxother = _IPC_EVIDENCE.maxstring


class DeliveryRequestRejected(ValueError):
    """Report bounded identity evidence without the body or full header dump."""


@contextmanager
def _socket_grant(socket_path, socket_group_id):
    """Hold one private directory lock before replacing its retired listener.

    Ports tools/provider_proxy.py::socket_grant at NMRNet 8e14575. This new
    listener needs no separate readiness file; its owner closes servers first.
    """
    shared = socket_group_id is not None

    # silent-defaults: allow-contract -- Directory access mode follows the explicit same-UID or shared-group socket grant.
    parent_modes = (0o710, 0o2710) if shared else (0o700,)

    # silent-defaults: allow-contract -- Socket mode follows the explicit same-UID or shared-group connection grant.
    socket_mode = 0o660 if shared else 0o600

    if shared and (socket_group_id != (daemon_gid := os.getegid())):
        raise DeliveryUnavailable(f"{socket_group_id=} should match {daemon_gid=}")

    socket_parent_stat = socket_path.parent.lstat()

    if (
        ((resolved_socket_parent := socket_path.parent.resolve()) != socket_path.parent) or
        (not stat.S_ISDIR(socket_parent_stat.st_mode)) or
        (socket_parent_stat.st_uid != os.geteuid()) or
        (stat.S_IMODE(socket_parent_stat.st_mode) not in parent_modes) or
        (shared and (socket_parent_stat.st_gid != socket_group_id))
    ):
        # silent-defaults: allow-contract -- Parent permission requirements follow the explicit shared-group or same-UID socket grant.
        required_parent_mode = "mode 0710 or 02710 with the configured socket GID; " if shared else "mode 0700; "

        raise DeliveryUnavailable(
            "Cannot open delivery IPC: require a canonical daemon-owned parent, "
            + required_parent_mode
            + f"{socket_path.parent=}, {resolved_socket_parent=}, {socket_parent_stat.st_uid=}, "
            + f"{socket_parent_stat.st_gid=}, {stat.S_IMODE(socket_parent_stat.st_mode)=:04o}, {os.geteuid()=}."
        )

    expected_lock_permissions = 0o600
    socket_lock_path = socket_path.parent / ".socket.lock"
    socket_lock_fd = os.open(socket_lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, expected_lock_permissions)
    listener = None

    try:
        socket_lock_stat = os.fstat(socket_lock_fd)

        if not stat.S_ISREG(socket_lock_stat.st_mode):
            raise DeliveryUnavailable(f"{socket_lock_path=} should be a regular file; {socket_lock_stat.st_mode=:#o}")

        if not (socket_lock_stat.st_uid == (daemon_uid := os.geteuid())):
            raise DeliveryUnavailable(f"{socket_lock_path=}: {socket_lock_stat.st_uid=} ≠ {daemon_uid=}")

        socket_lock_permissions = stat.S_IMODE(socket_lock_stat.st_mode)

        if not (socket_lock_permissions == expected_lock_permissions):
            raise DeliveryUnavailable(f"{socket_lock_path=}: {socket_lock_permissions=:#o} ≠ {expected_lock_permissions=:#o}")

        fcntl.flock(socket_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            previous_socket_stat = socket_path.lstat()
        except FileNotFoundError:
            previous_socket_stat = None

        if previous_socket_stat is not None:
            if not stat.S_ISSOCK(previous_socket_stat.st_mode):
                raise DeliveryUnavailable(f"{socket_path=} should be a socket; {previous_socket_stat.st_mode=:#o}")

            if not (previous_socket_stat.st_uid == (daemon_uid := os.geteuid())):
                raise DeliveryUnavailable(f"{socket_path=}: {previous_socket_stat.st_uid=} ≠ {daemon_uid=}")

            previous_socket_permissions = stat.S_IMODE(previous_socket_stat.st_mode)

            if not (previous_socket_permissions == socket_mode):
                raise DeliveryUnavailable(f"{socket_path=}: {previous_socket_permissions=:#o} ≠ {socket_mode=:#o}")

            if shared and (previous_socket_stat.st_gid != socket_group_id):
                raise DeliveryUnavailable(f"{socket_path=}: {previous_socket_stat.st_gid=} ≠ {socket_group_id=}")

            socket_path.unlink()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        # Grant group connect access at birth; credentials and database stay private.
        # silent-defaults: allow-contract -- Birth umask enforces the selected private or shared-group socket posture.
        previous_umask = os.umask(0o117 if shared else 0o177)

        try:
            listener.bind(str(socket_path))
        finally:
            os.umask(previous_umask)

        owned_socket_stat = socket_path.lstat()
        owned_socket_identity = (owned_socket_stat.st_dev, owned_socket_stat.st_ino)

        listener.listen(MAX_CONNECTIONS)
        listener.setblocking(False)
        yield listener
        current_socket_stat = socket_path.lstat()
        current_socket_identity = (current_socket_stat.st_dev, current_socket_stat.st_ino)

        if current_socket_identity != owned_socket_identity:
            raise DeliveryUnavailable(f"{socket_path=}: {current_socket_identity=} ≠ {owned_socket_identity=}; replacement left untouched")

        socket_path.unlink()
    finally:
        if listener is not None:
            listener.close()

        os.close(socket_lock_fd)


class DeliveryInbox:
    """Own listeners, client handlers and original offloaded SQLite operations."""

    max_request_bytes = MAX_COMPLETE_REQUEST_BYTES

    def __init__(self, queue, endpoints, *, request_seconds, on_accepted, on_failure, binding_status):
        (self.queue, self.endpoints) = (queue, endpoints)
        self.request_seconds = request_seconds

        (self.on_accepted, self.on_failure) = (on_accepted, on_failure)
        self.binding_status = binding_status

        (self._grants, self._servers) = (ExitStack(), [])
        (self._clients, self._storage_tasks) = (set(), set())
        (self.failed, self.failure) = (asyncio.Event(), None)

    async def start(self):
        try:
            for endpoint in self.endpoints:
                try:
                    listener = self._grants.enter_context(_socket_grant(endpoint.socket_path, endpoint.socket_group_id))

                    server = await asyncio.start_unix_server(
                        lambda reader, writer, selected=endpoint: self._client(selected, reader, writer),
                        sock=listener,
                        limit=MAX_HEADER_BYTES,
                    )
                except BaseException as cause:
                    self.on_failure(
                        cause,
                        {
                            "event": "delivery_binding_start_failed",
                            "api_origin": endpoint.api_origin,
                            "provider_ref": endpoint.provider_ref,
                            "socket_path": str(endpoint.socket_path),
                        }
                    )

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

        if (error is not None) and (not isinstance(error, (DeliveryFull, DeliveryConflict, DeliveryInventoryUnavailable))):
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
        (ipc_request, body) = (None, bytearray())

        async with asyncio.timeout(self.request_seconds):
            while True:
                event = connection.next_event()

                if event is h11.NEED_DATA:
                    connection.receive_data(await reader.read(65536))
                elif isinstance(event, h11.Request):
                    ipc_request = event
                elif isinstance(event, h11.Data):
                    request_body_size_bytes = len(body) + len(event.data)

                    if request_body_size_bytes > self.max_request_bytes:
                        raise DeliveryRequestRejected(f"{request_body_size_bytes=} should be at most {self.max_request_bytes=}")

                    body.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    return (ipc_request, bytes(body))
                # repo-check: retain-type-guard -- This protocol event means the peer closed before completing its request
                elif isinstance(event, h11.ConnectionClosed):
                    raise ConnectionError(f"{type(event)=} arrived before request completion; {len(body)=}")

    def _headers(self, ipc_request, endpoint):
        identity_headers = {}

        for (header_name, header_value) in ipc_request.headers:
            if header_name in (b"nmr-api-origin", b"nmr-provider-ref", b"nmr-job-ref"):
                if header_name in identity_headers:
                    raise DeliveryRequestRejected(f"{header_name=} should appear only once")

                try:
                    identity_headers[header_name] = header_value.decode("ascii")
                except UnicodeDecodeError as cause:
                    raise DeliveryRequestRejected(f"{header_name=} should contain ASCII text") from cause

        claimed_api_origin = identity_headers.get(b"nmr-api-origin")
        claimed_provider_ref = identity_headers.get(b"nmr-provider-ref")
        expected_api_origin = endpoint.api_origin
        expected_provider_ref = endpoint.provider_ref

        if not (claimed_api_origin == expected_api_origin):
            raise DeliveryRequestRejected(f"{_IPC_EVIDENCE.repr(claimed_api_origin)=} ≠ {expected_api_origin=}")

        if not (claimed_provider_ref == expected_provider_ref):
            raise DeliveryRequestRejected(f"{_IPC_EVIDENCE.repr(claimed_provider_ref)=} ≠ {expected_provider_ref=}")

        return identity_headers

    async def _dispatch(self, endpoint, ipc_request, body):
        identity_headers = self._headers(ipc_request, endpoint)

        try:
            route_segments = ipc_request.target.decode("ascii").split("/")
        except UnicodeDecodeError as cause:
            raise DeliveryRequestRejected(f"Cannot decode {len(ipc_request.target)=} as ASCII") from cause

        if (ipc_request.method == b"POST") and (len(route_segments) == 4) and (route_segments[1] == "submit"):
            try:
                delivery_outcome = DeliveryOutcome(
                    endpoint.api_origin,
                    endpoint.provider_ref,
                    identity_headers.get(b"nmr-job-ref"),
                    route_segments[3],
                    TerminalOperation(route_segments[2]),
                    body,
                )
            except (TypeError, ValueError) as cause:
                raise DeliveryRequestRejected("Cannot admit the delivery outcome") from cause

            accepted = await self.storage(self.queue.enqueue, delivery_outcome)

            self.on_accepted(endpoint)

            return accepted

        if (ipc_request.method != b"GET") or body:
            raise DeliveryRequestRejected(f"{_IPC_EVIDENCE.repr(ipc_request.method)=} and {len(body)=} should describe a bodyless GET")

        if route_segments == ["", "pending"]:
            rows = await self.storage(self.queue.ipc_inventory, endpoint.api_origin, endpoint.provider_ref)

            return {
                "api_origin": endpoint.api_origin,
                "provider_ref": endpoint.provider_ref,
                "items": rows,
                "capacity": {"provider_items": self.queue.limits.provider_items, "provider_bytes": self.queue.limits.provider_bytes},
                "publisher": self.binding_status(endpoint),
            }

        if (len(route_segments) == 3) and (route_segments[1] == "status"):
            try:
                require_attempt_ref(route_segments[2])
            except ValueError as cause:
                raise DeliveryRequestRejected("Cannot read delivery status") from cause

            return await self.storage(self.queue.status, endpoint.api_origin, endpoint.provider_ref, route_segments[2])

        raise DeliveryRequestRejected(f"{_IPC_EVIDENCE.repr(ipc_request.target)=} should select submission, pending inventory, or status")

    async def _respond(self, connection, writer, status, response_payload):
        content = json.dumps(response_payload, allow_nan=False, separators=(",", ":")).encode("utf-8")

        writer.write(
            connection.send(
                h11.Response(
                    status_code=status,
                    headers=[
                (b"Content-Type", b"application/json"),
                (b"Content-Length", str(len(content)).encode()),
                (b"Connection", b"close")
        ]
                )
            )
        )

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

            (ipc_request, body) = await self._read(reader, connection)

            try:
                accepted = await self._dispatch(endpoint, ipc_request, body)
                durable_enqueue_confirmed = ipc_request.method == b"POST"
                (status, response_payload) = (200, accepted)
            except DeliveryFull:
                (status, response_payload) = (429, {"error": "Delivery capacity is full; no new item was accepted."})
            except DeliveryConflict:
                (status, response_payload) = (409, {"error": "The original outstanding terminal request remains unchanged."})
            except DeliveryStorageFailed:
                (status, response_payload) = (503, {"error": "Delivery storage is unavailable; acceptance is not confirmed."})
            except DeliveryInventoryUnavailable as cause:
                self.on_failure(cause, {"event": "delivery_inventory_unavailable", "provider_ref": endpoint.provider_ref})
                (status, response_payload) = (503, {"error": "Pending inventory is oversized; publication continues and per-Attempt status remains available."})
            except DeliveryRequestRejected as cause:
                self.on_failure(cause, {"event": "delivery_request_rejected", "provider_ref": endpoint.provider_ref})
                (status, response_payload) = (400, {"error": "The request does not match this provider's delivery protocol."})

            await self._respond(connection, writer, status, response_payload)
        except (OSError, ConnectionError, TimeoutError, h11.ProtocolError) as cause:
            self.on_failure(
                cause,
                {
                    "event": "delivery_ipc_failed",
                    "provider_ref": endpoint.provider_ref,
                    "durable_enqueue_confirmed": durable_enqueue_confirmed,
                }
            )
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
                task_outcomes = await asyncio.gather(*(asyncio.shield(task) for task in tuple(tasks)), return_exceptions=True)

                failures.extend(task_outcome for task_outcome in task_outcomes if isinstance(task_outcome, BaseException))

        self._grants.close()

        # repo-check: allow-literal-guard-error -- Each child retains its original failure evidence; the label names their shared retirement boundary
        if failures:
            raise BaseExceptionGroup("Delivery IPC retirement failed after all original operations settled.", failures)
