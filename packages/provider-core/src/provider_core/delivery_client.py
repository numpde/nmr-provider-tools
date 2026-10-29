"""Transfer final NMR API requests through one provider's private delivery grant."""

from provider_core.container_guard import require_container

require_container()

import asyncio
import errno
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import httpcore
import httpx

from provider_core.attempt_request import require_provider_ref
from provider_core.attempt_response import require_attempt_ref
from provider_core.delivery_store import DeliveryConflict, DeliveryFull
from provider_core.response_json import ResponseRejected, read_document
from provider_core.signing import _https_origin
from provider_core.transport import ExchangeFailed, require_unix_socket_path, temporary_io_failure
from provider_core.unix_http import DELIVERY_ORIGIN, UnixHttpTransport
from provider_core.upload_response import require_job_ref

# Bound complete local-delivery status and acknowledgement responses.
# repo-check: allow-module-constant
MAX_IPC_RESPONSE_BYTES = 2097152


# silent-defaults: allow-contract -- Production retains its existing shared volume names; a contained recipe may supply its admitted private project.
def delivery_volume_name(api_origin, provider_ref, *, namespace="nmr-api-delivery"):
    """Name the shared socket grant for this exact authenticated binding."""
    if _https_origin(api_origin)[0] != api_origin:
        raise ValueError("Delivery requires the configured canonical API origin.")
    require_provider_ref(provider_ref)
    return namespace + "-ipc-" + hashlib.sha256((api_origin + "\0" + provider_ref).encode()).hexdigest()


class DeliveryRejected(RuntimeError):
    """No valid durable acknowledgement was obtained from this request."""


class DeliveryUnavailable(RuntimeError):
    """Keep the exact outcome; this failure is not permission to send it elsewhere."""

    # A local service refusal has a status; transport interruption instead supplies a protected cause without a received status.
    def __init__(self, message, *, cause=None, status=None):
        super().__init__(message)
        self.cause, self.status = cause, status


@dataclass(frozen=True, slots=True)
class DeliveryEndpoint:
    api_origin: str
    provider_ref: str
    socket_path: Path
    # Direct same-UID embeddings may retain private IPC; deployed shared bindings explicitly grant a socket group.
    socket_group_id: int | None = None

    def __post_init__(self):
        if _https_origin(self.api_origin)[0] != self.api_origin:
            raise ValueError("Delivery requires the configured canonical API origin.")
        require_provider_ref(self.provider_ref)
        require_unix_socket_path(str(self.socket_path))
        if self.socket_group_id is not None and (
            type(self.socket_group_id) is not int or not 1 <= self.socket_group_id <= 4294967294
        ):
            raise ValueError("Delivery socket group must be an explicit nonzero GID.")


class DeliveryClient:
    """One serial IPC owner; producer cleanup and API publication are separate."""

    def __init__(self, endpoint, *, exchange_seconds, retirement_seconds):
        self.endpoint = endpoint
        self._transport = UnixHttpTransport(
            socket_path=str(endpoint.socket_path), exchange_seconds=exchange_seconds, retirement_seconds=retirement_seconds
        )
        self.last_failure = None

    @property
    def retirement_confirmed(self):
        return self._transport.retirement_confirmed

    async def retire(self):
        await self._transport.retire(owner=self)

    # silent-defaults: allow-contract -- Status reads have no body or Job header; submission supplies both explicitly.
    async def _request(self, method, path, *, body=b"", job_ref=None):
        submitting = method == "POST"
        headers = {
            "NMR-API-Origin": self.endpoint.api_origin,
            "NMR-Provider-Ref": self.endpoint.provider_ref,
            "Content-Type": "application/json",
        }
        if job_ref is not None:
            headers["NMR-Job-Ref"] = job_ref
        self.last_failure = None
        try:
            response = await self._transport.exchange(
                httpx.Request(method, DELIVERY_ORIGIN + path, headers=headers, content=body),
                maximum_bytes=MAX_IPC_RESPONSE_BYTES, owner=self,
            )
        except asyncio.CancelledError as interruption:
            # silent-defaults: allow-contract -- Interrupted POST handoff retains uncertainty; an interrupted status read requested no enqueue.
            self.last_failure = DeliveryUnavailable(
                ("Local delivery acknowledgement was interrupted; retain the original outcome and reconcile its handoff."
                 if submitting else "The delivery status read was interrupted; no enqueue was requested."),
                cause=self._transport.last_failure,
            )
            raise interruption from self.last_failure
        except ExchangeFailed as cause:
            # silent-defaults: allow-contract -- POST exchange failure retains the exact outcome; a status-read failure requests only transport retirement.
            self.last_failure = DeliveryUnavailable(
                ("Cannot confirm local delivery acceptance. Retain the identical outcome; settle this connection before retrying."
                 if submitting else "Cannot read delivery status; settle this connection before retrying the read."), cause=cause
            )
            raise self.last_failure from None
        if submitting and response.status == 429:
            raise DeliveryFull("Delivery capacity is full. Retain this outcome, wait for space and stop admitting computation.")
        if submitting and response.status == 409:
            raise DeliveryConflict("The relay retains a different terminal request for this Attempt; reconcile it without replacing either choice.")
        if response.status == 503:
            # silent-defaults: allow-contract -- A refused POST cannot confirm acceptance; a refused read reports unavailable metadata.
            self.last_failure = DeliveryUnavailable(
                ("The local delivery service cannot accept work; retain this outcome and inspect its operator status."
                 if submitting else "Delivery status is unavailable; inspect the service's operator diagnostics."), status=503
            )
            raise self.last_failure
        if response.status != 200:
            raise DeliveryRejected(f"The local delivery service refused this request (HTTP {response.status}); inspect its operator diagnostics.")
        try:
            return read_document(response.body, MAX_IPC_RESPONSE_BYTES)
        except ResponseRejected as cause:
            # silent-defaults: allow-contract -- An invalid POST acknowledgement preserves enqueue uncertainty; an invalid read supplies no status evidence.
            message = (
                "The local delivery service returned an invalid acknowledgement; retain the original outcome."
                if submitting else "The local delivery service returned invalid status metadata."
            )
            raise DeliveryRejected(message) from cause

    async def submit(self, item):
        if item.key[:2] != (self.endpoint.api_origin, self.endpoint.provider_ref):
            raise ValueError("The accepted outcome belongs to another configured delivery binding.")
        reply = await self._request(
            "POST", f"/submit/{item.operation.value}/{item.execution_attempt_ref}", body=item.body, job_ref=item.job_ref
        )
        if reply != item.acceptance():
            raise DeliveryRejected("The durable acknowledgement does not match this exact provider, Attempt, operation and body digest.")
        return reply

    def _metadata(self, value):
        fields = {"job_ref", "execution_attempt_ref", "operation", "body_sha256", "body_bytes", "state"}
        if type(value) is not dict or not fields <= value.keys():
            raise DeliveryRejected("Delivery metadata is missing its Job, Attempt or retained-body identity.")
        try:
            require_attempt_ref(value["execution_attempt_ref"])
            require_job_ref(value["job_ref"])
        except ValueError as cause:
            raise DeliveryRejected("Delivery metadata has an invalid Job or Attempt identity.") from cause
        if (value["operation"] not in ("complete", "fail") or value["state"] not in ("pending", "held")
                or type(value["body_sha256"]) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value["body_sha256"]) is None
                or type(value["body_bytes"]) is not int or value["body_bytes"] <= 0):
            raise DeliveryRejected("Delivery metadata has an unsupported operation, state or body identity.")
        return value

    async def pending(self):
        reply = await self._request("GET", "/pending")
        if (type(reply) is not dict or not {"api_origin", "provider_ref", "items", "capacity", "publisher"} <= reply.keys()
                or (reply["api_origin"], reply["provider_ref"]) != (self.endpoint.api_origin, self.endpoint.provider_ref)
                or type(reply["items"]) is not list):
            raise DeliveryRejected("The pending inventory does not belong to the configured delivery binding.")
        publisher = reply["publisher"]
        if (type(publisher) is not dict
                or publisher.get("state") not in ("inbox_only", "starting", "running", "paused", "stopping", "stopped")):
            raise DeliveryRejected("Delivery inventory has an unsupported publisher state.")
        # Diagnostic text/counters are display facts. Their shape must not
        # prevent ownership or capacity observation; they grant no admission.
        capacity = reply["capacity"]
        if (type(capacity) is not dict or not {"provider_items", "provider_bytes"} <= capacity.keys()
                or any(type(capacity[name]) is not int or capacity[name] <= 0 for name in ("provider_items", "provider_bytes"))):
            raise DeliveryRejected("Delivery inventory requires positive item and byte allowances.")
        for item in reply["items"]:
            self._metadata(item)
        return reply

    async def status(self, execution_attempt_ref):
        require_attempt_ref(execution_attempt_ref)
        reply = await self._request("GET", f"/status/{execution_attempt_ref}")
        if reply is None:
            return None
        result = self._metadata(reply)
        if result["execution_attempt_ref"] != execution_attempt_ref:
            raise DeliveryRejected("The delivery status names another Attempt.")
        return result

    def can_retry(self, failure):
        """Permit another local read or unchanged submission after owned I/O settles."""
        if failure is not self.last_failure or type(failure) is not DeliveryUnavailable or not self.retirement_confirmed:
            return False
        if failure.status == 503:
            return True
        exchange = failure.cause
        if (type(exchange) is not ExchangeFailed or exchange.cleanup_failures
                or isinstance(exchange.interruption, asyncio.CancelledError)):
            return False
        primary = exchange.primary
        connect_cause = None
        if type(primary) is httpcore.ConnectError:
            # httpcore 1.0.9's pool clears the mapped Unix error's cause:
            # https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/connection_pool.py#L248-L256
            # The native error remains in __context__. An explicit cause wins:
            # incidental missing-file context cannot override a TLS refusal.
            connect_cause = primary.__cause__
            if connect_cause is None:
                connect_cause = primary.__context__
        return (
            temporary_io_failure(primary, 8)
            or (type(connect_cause) is FileNotFoundError and connect_cause.errno == errno.ENOENT)
            or (type(primary) is httpcore.RemoteProtocolError
                and (exchange.response_head is None or exchange.response_head.status in (200, 503)))
        )
