"""Own one bounded HTTP exchange and its retirement before admitting another.

Each exchange uses a fresh client. Source precedent: Magnet provider_http._send
at the revision pinned in README. Unlike its best-effort response cleanup, failed
or unfinished cleanup here remains owned and blocks subsequent exchanges.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import errno
import fcntl
import hashlib
import math
import os
import re
import ssl
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpcore

from provider_core.network import OwnedNetworkBackend


class ExchangeFailed(RuntimeError):
    """Safe public failure text with original exceptions for protected diagnostics.

    primary, cleanup_failures and response_head may contain private data.
    Recipients must use a protected diagnostic sink, never render them directly.
    response_head describes the origin reply; proxy_connect_status is the
    separately received CONNECT status, never an API receipt.
    Transport failure does not establish whether a server mutation committed.
    """

    # silent-defaults: allow-contract -- No observed cleanup failure is an empty tuple, separate from primary and interruption evidence.
    def __init__(self, message, *, primary=None, cleanup_failures=(), interruption=None, response_head=None, proxy_connect_status=None):
        super().__init__(message)
        self.primary = primary
        self.cleanup_failures = tuple(cleanup_failures)
        self.interruption = interruption
        self.response_head = response_head
        self.proxy_connect_status = proxy_connect_status


@dataclass(frozen=True, slots=True)
class HttpResponseHead:
    """Received protocol metadata, without a complete body or admission claim.

    The exchange owner retains this private evidence across body/cleanup failure;
    API correlation/topology interpretation belongs to ProviderAPI. The existing
    pinned httpcore/h11 parser still owns HTTP framing and its header limits.
    Parser source: httpcore 1.0.9, httpcore/_async/http11.py:45-63,170-199;
    sibling correlation rationale: SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    src/secs_inference/provider/http.py:290-305. See note003 R7a/R7b.
    """

    status: int
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Complete raw response bytes; no automatic decoding or status interpretation."""

    status: int
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class HttpFileResponse:
    """Complete raw transfer facts, without Upload admission or file finalization.

    The caller owns the borrowed descriptor and its contents. These observations
    describe the body written during this exchange, not future pathname bytes.
    """

    status: int
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)
    byte_length: int
    content_hash: str


def _require_output_file(descriptor):
    try:
        facts = os.fstat(descriptor)
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    except OSError as cause:
        raise ExchangeFailed(
            "Cannot receive HTTP response in a file: output descriptor inspection failed before sending the request",
            primary=cause,
        ) from None
    access_mode = flags & os.O_ACCMODE
    if not stat.S_ISREG(facts.st_mode) or access_mode not in (os.O_WRONLY, os.O_RDWR) or flags & os.O_APPEND:
        raise ExchangeFailed(
            "Cannot receive HTTP response in a file: output must be a writable regular file without append mode; request was not sent"
        )


def _write_file_chunk(descriptor, chunk):
    remaining = memoryview(chunk)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "Cannot write HTTP response: file write made no progress")
        remaining = remaining[written:]


def _observe_file_write(waiter):
    if not waiter.cancelled():
        waiter.exception()


class _FileBody:
    """One bounded disk write at a time, borrowing the caller's descriptor.

    A cancelled waiter never cancels the original disk operation or permits
    descriptor reuse before that same operation settles.
    This executor handles response writes only; it is not another storage queue.
    """

    def __init__(self, descriptor):
        self._descriptor = descriptor
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-response")
        self._pending = None
        self.byte_length = 0
        self.digest = hashlib.sha256()

    @property
    def pending(self):
        return self._pending is not None

    async def validate(self):
        await self._run(_require_output_file, self._descriptor)

    async def write(self, chunk):
        for offset in range(0, len(chunk), 65536):
            await self._run(_write_file_chunk, self._descriptor, memoryview(chunk)[offset : offset + 65536])
        self.byte_length += len(chunk)
        self.digest.update(chunk)

    async def _run(self, operation, *args):
        # Register the original future before yielding. File admission and each
        # bounded write finish before network work can advance to its next phase.
        worker = self._executor.submit(operation, *args)
        waiter = asyncio.wrap_future(worker)
        waiter.add_done_callback(_observe_file_write)
        self._pending = worker, waiter
        await self.settle()

    async def settle(self):
        worker, waiter = self._pending
        await asyncio.wait({waiter})
        self._pending = None
        worker.result()

    def shutdown(self):
        # Every submitted effect is already observed; the borrowed fd stays open.
        self._executor.shutdown(wait=False)


def response_request_id(headers):
    """Project one bounded visible-ASCII correlation ID, never other headers.

    Moved unchanged from api.py::_header_request_id so API and protected model
    diagnostics share the reviewed owner. Authority: the API revision pinned in
    api.py, nmr_api/request_ids.py. A received ID proves no API/model effect.
    """
    values = [value for name, value in headers if name.lower() == b"x-request-id"]
    if len(values) == 1 and re.fullmatch(rb"[!-~]{1,128}", values[0]) is not None:
        return values[0].decode("ascii")
    return None


@dataclass(slots=True, repr=False)
class _Outcome:
    response: HttpResponse | HttpFileResponse | None = None
    response_head: HttpResponseHead | None = None
    proxy_connect_status: int | None = None
    primary: BaseException | None = None
    cleanup_failures: list[BaseException] = field(default_factory=list)


@dataclass(slots=True, repr=False)
class _Exchange:
    """Keep received evidence reachable while this exact task still owns cleanup."""

    task: asyncio.Task
    outcome: _Outcome
    owner: object


def _positive_seconds(value):
    if not (type(value) in (int, float) and math.isfinite(value) and 0 < value):
        raise ValueError("Cannot configure HTTP exchange: time limits must be finite positive seconds")
    return value


def require_unix_socket_path(path):
    """Admit the pathname limit shared by the two explicit Unix transports."""
    if type(path) is not str or not path.startswith("/") or "\0" in path:
        raise ValueError("Cannot configure Unix socket: supply an absolute UTF-8 pathname without NUL.")
    try:
        size = len(path.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Cannot configure Unix socket: its pathname must be valid UTF-8.") from None
    # Linux sun_path[108] includes its terminating NUL.
    if not 1 <= size <= 107:
        raise ValueError("Cannot configure Unix socket: its pathname must fit Linux's 107-byte limit.")


def require_transport_settings(*, trust, exchange_seconds, retirement_seconds, proxy_socket=None):
    """Admit TLS and deadlines before composition acquires any transport."""
    if not isinstance(trust, ssl.SSLContext) or trust.verify_mode != ssl.CERT_REQUIRED or not trust.check_hostname:
        raise ValueError("Cannot configure HTTPS exchange: TLS must verify the certificate and hostname")
    _positive_seconds(exchange_seconds)
    _positive_seconds(retirement_seconds)
    if proxy_socket is not None:
        require_unix_socket_path(proxy_socket)


class HttpTransport:
    """Serial exchange owner with a total deadline and additional cleanup grace.

    The composition owns this object and must await retire() before replacement
    or shutdown. An unconfirmed retirement requires enclosing process retirement;
    constructing another owner does not discharge the old owner's obligations.
    An explicit proxy_socket routes HTTPS CONNECT through that fixed Unix path;
    None selects direct TCP. A configured proxy never falls back to direct I/O.
    """

    def __init__(self, *, trust, exchange_seconds, retirement_seconds, proxy_socket=None):
        require_transport_settings(
            trust=trust, exchange_seconds=exchange_seconds, retirement_seconds=retirement_seconds, proxy_socket=proxy_socket
        )
        self._trust = trust
        self._exchange_seconds = exchange_seconds
        self._retirement_seconds = retirement_seconds
        self._proxy_socket = proxy_socket
        self._active = None
        self.last_failure = None

    @property
    def exchange_seconds(self):
        return self._exchange_seconds

    @property
    def retirement_seconds(self):
        return self._retirement_seconds

    @property
    def retirement_confirmed(self):
        """Whether no exchange or unresolved cleanup remains owned here."""
        return self._active is None

    @property
    def pending_owner(self):
        """Caller whose admitted exchange still owns I/O or unresolved cleanup."""
        # silent-defaults: allow-contract -- No active exchange means there is no pending I/O owner.
        return None if self._active is None else self._active.owner

    def _connection_pool(self, options):
        if self._proxy_socket is None:
            return httpcore.AsyncConnectionPool(**options)
        # Fixed httpcore metadata; OwnedNetworkBackend admits only the Unix
        # proxy. CONNECT and TLS retain the destination's original authority.
        return httpcore.AsyncHTTPProxy(proxy_url="http://unix-proxy.invalid", **options)

    def _require_destination(self, request):
        if request.url.scheme != "https":
            raise ValueError("Cannot start HTTPS exchange: destination must use HTTPS")
        # httpcore 1.0.9 emits an unbracketed IPv6 CONNECT authority.
        if self._proxy_socket is not None and b":" in request.url.raw_host:
            raise ValueError(
                "Cannot start proxied HTTPS exchange: IPv6 literal destinations are not supported; use a DNS name or IPv4 address."
            )

    async def _perform(self, request, maximum_bytes, account_bytes, outcome, output_fd):
        client = None
        response = None
        # silent-defaults: allow-contract -- No output descriptor selects the buffered response; a supplied descriptor selects file output.
        file_body = None if output_fd is None else _FileBody(output_fd)
        network = None
        try:
            if file_body is not None:
                await file_body.validate()
            network = OwnedNetworkBackend(proxy_socket=self._proxy_socket)
            options = {
                "ssl_context": self._trust,
                "network_backend": network,
                "max_connections": 1,
                "max_keepalive_connections": 0,
                "retries": 0,
            }
            client = self._connection_pool(options)
            extensions = {"timeout": dict.fromkeys(("connect", "read", "write", "pool"), self._exchange_seconds)}
            if self._proxy_socket is not None:
                async def observe_connect(name, info):
                    # httpcore 1.0.9 forwards extensions into CONNECT, whose
                    # parsed head is the first HTTP/1.1 response. Retain only
                    # its status before ProxyError discards that structure.
                    # https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/http_proxy.py#L278-L297
                    # https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/http11.py#L98-L114
                    if name == "http11.receive_response_headers.complete" and outcome.proxy_connect_status is None:
                        outcome.proxy_connect_status = info["return_value"][1]

                extensions["trace"] = observe_connect
            wire_request = httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme, host=request.url.raw_host, port=request.url.port, target=request.url.raw_path
                ),
                headers=request.headers.raw,
                content=request.content,
                extensions=extensions,
            )
            response = await client.handle_async_request(wire_request)
            outcome.response_head = HttpResponseHead(response.status, tuple(response.headers))
            # silent-defaults: allow-contract -- Only a buffered response owns an in-memory body; file mode does not buffer it.
            body = bytearray() if file_body is None else None
            received_bytes = 0
            async for chunk in response.aiter_stream():
                # Acquisition charges consumed work even if this chunk makes
                # the response unusable. Accounting is synchronous and bounded.
                if account_bytes is not None:
                    account_bytes(len(chunk))
                received_bytes += len(chunk)
                if received_bytes > maximum_bytes:
                    raise ExchangeFailed(f"Cannot receive HTTP response: raw body exceeds the {maximum_bytes}-byte limit")
                if file_body is None:
                    body.extend(chunk)
                else:
                    await file_body.write(chunk)
            head = outcome.response_head
            if file_body is None:
                outcome.response = HttpResponse(head.status, head.headers, bytes(body))
            else:
                outcome.response = HttpFileResponse(
                    head.status, head.headers, file_body.byte_length, "sha256:" + file_body.digest.hexdigest()
                )
        except BaseException as failure:
            # The owner observes this task separately, so cancellation cannot
            # discard either the primary cause or ownership of unfinished I/O.
            outcome.primary = failure
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except BaseException as failure:
                    outcome.cleanup_failures.append(failure)
            if network is not None:
                try:
                    await network.aclose()
                except BaseException as failure:
                    outcome.cleanup_failures.append(failure)
            if client is not None:
                try:
                    await client.aclose()
                except BaseException as failure:
                    outcome.cleanup_failures.append(failure)
            if file_body is not None:
                # Stop network I/O first, then observe the same disk future. A
                # blocked write keeps _perform and this transport's owner alive.
                while file_body.pending:
                    try:
                        await file_body.settle()
                    except asyncio.CancelledError as failure:
                        if outcome.primary is None:
                            outcome.primary = failure
                    except BaseException as failure:
                        if outcome.primary is None or isinstance(outcome.primary, asyncio.CancelledError):
                            outcome.primary = failure
                        else:
                            outcome.cleanup_failures.append(failure)
                file_body.shutdown()
        return outcome

    async def _observe_retirement(self, task):
        """Observe without cancelling cleanup, even if the caller cancels again."""
        deadline = asyncio.get_running_loop().time() + self._retirement_seconds
        cancellation = None
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError as failure:
                cancellation = failure
        return cancellation

    def _completed(self, exchange):
        task = exchange.task
        if not task.done():
            return None
        # _perform catches cancellation after entry. A cancelled task here
        # therefore never entered its body and acquired no client or response.
        outcome = _Outcome(primary=asyncio.CancelledError()) if task.cancelled() else task.result()
        if not outcome.cleanup_failures and self._active is exchange:
            self._active = None
        return outcome

    async def exchange(self, request, *, maximum_bytes, account_bytes=None, owner=None):
        """Return only complete responses after cleanup; preserve cancellation."""
        return await self._exchange(
            request, maximum_bytes=maximum_bytes, account_bytes=account_bytes, owner=owner, output_fd=None
        )

    async def exchange_file(self, request, *, output_fd, maximum_bytes, account_bytes=None, owner=None):
        """Write a body without buffering it; return raw count/hash after retirement.

        The caller lends an exclusively used writable regular file descriptor
        without append mode, at its current position. Keep it open and unchanged
        until retirement_confirmed,
        including after failure or cancellation. This transport never closes or
        unlinks the file. Failure may leave partial or complete response bytes;
        their presence does not admit an Upload. Acquisition owns verification
        and finalization.
        """
        if not (type(output_fd) is int and 0 <= output_fd):
            raise ValueError("Cannot receive HTTP response in a file: output_fd must be a non-negative descriptor")
        return await self._exchange(
            request, maximum_bytes=maximum_bytes, account_bytes=account_bytes, owner=owner, output_fd=output_fd
        )

    async def _exchange(self, request, *, maximum_bytes, account_bytes, owner, output_fd):
        if self._active is not None:
            raise ExchangeFailed("Cannot start HTTP exchange: an earlier exchange is active or its retirement is unconfirmed")
        if not (type(maximum_bytes) is int and 0 <= maximum_bytes):
            raise ValueError("Cannot receive HTTP response: byte limit must be a non-negative integer")
        self._require_destination(request)
        self.last_failure = None
        received = _Outcome()
        task = asyncio.create_task(
            self._perform(request, maximum_bytes, account_bytes, received, output_fd), name="provider-core-http-exchange"
        )
        exchange = _Exchange(task, received, owner)
        self._active = exchange
        primary = None
        cancellation = None
        try:
            done, _ = await asyncio.wait({task}, timeout=self._exchange_seconds)
            if not done:
                primary = TimeoutError("HTTP exchange exceeded its aggregate deadline")
        except asyncio.CancelledError as failure:
            primary = cancellation = failure
        if not task.done():
            if not task.cancelling():
                task.cancel()
            later_cancellation = await self._observe_retirement(task)
            if later_cancellation is not None:
                cancellation = later_cancellation
        outcome = self._completed(exchange)
        interruption = None
        if received.primary is not None and not isinstance(received.primary, asyncio.CancelledError):
            interruption = primary
            primary = received.primary
        if outcome is not None and primary is None:
            primary = outcome.primary
        cleanup_failures = received.cleanup_failures
        if primary is None and outcome is not None and not cleanup_failures:
            return outcome.response
        if outcome is None or cleanup_failures:
            message = "Cannot complete HTTP exchange: resource retirement is unconfirmed; this transport cannot accept more work"
        elif isinstance(primary, TimeoutError):
            message = "Cannot complete HTTP exchange: the aggregate deadline expired; resources were retired"
        elif isinstance(primary, ExchangeFailed):
            message = str(primary)
        else:
            message = "Cannot complete HTTP exchange: request or response I/O failed; resources were retired"
        if isinstance(interruption, TimeoutError):
            message += "; the aggregate deadline also expired"
        failure = ExchangeFailed(
            message,
            primary=primary,
            cleanup_failures=cleanup_failures,
            interruption=interruption,
            response_head=received.response_head,
            proxy_connect_status=received.proxy_connect_status,
        )
        self.last_failure = failure
        if cancellation is not None or isinstance(primary, asyncio.CancelledError):
            raise cancellation if cancellation is not None else primary
        if isinstance(primary, (KeyboardInterrupt, SystemExit)):
            raise primary
        raise failure from None

    async def retire(self, *, owner=None):
        """Stop outstanding I/O and confirm closure; retain failed obligations."""
        exchange = self._active
        if exchange is None:
            return
        if exchange.owner is not owner:
            raise ExchangeFailed("Cannot retire HTTP exchange: the outstanding operation belongs to another caller")
        task = exchange.task
        if not task.done() and not task.cancelling():
            task.cancel()
        cancellation = await self._observe_retirement(task)
        outcome = self._completed(exchange)
        observed = outcome if outcome is not None else exchange.outcome
        if self.last_failure is not None:
            self.last_failure.response_head = observed.response_head
            self.last_failure.proxy_connect_status = observed.proxy_connect_status
            # Late retirement augments the retained exchange evidence; a pending
            # task or its expected cancellation must not erase the original cause.
            if (
                observed.primary is not None
                and not isinstance(observed.primary, asyncio.CancelledError)
                and self.last_failure.primary is not observed.primary
            ):
                self.last_failure.interruption = self.last_failure.primary
                self.last_failure.primary = observed.primary
            self.last_failure.cleanup_failures = tuple(observed.cleanup_failures)
        if outcome is None or outcome.cleanup_failures:
            if self.last_failure is None:
                self.last_failure = ExchangeFailed(
                    "Cannot retire HTTP transport: resource closure remains unconfirmed; retire the enclosing process before replacement",
                    primary=observed.primary,
                    cleanup_failures=observed.cleanup_failures,
                    response_head=observed.response_head,
                    proxy_connect_status=observed.proxy_connect_status,
                )
            if cancellation is None:
                raise self.last_failure from None
        if cancellation is not None:
            raise cancellation
