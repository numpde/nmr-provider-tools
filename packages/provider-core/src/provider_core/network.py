"""Retain TCP streams across TLS upgrade until their closure succeeds.

httpcore 1.0.9's AnyIOStream.start_tls catches Exception, not cancellation;
its connection object does not retain the raw stream when TLS is interrupted. Own the
raw stream through the public backend interface to close that gap. Source:
https://github.com/encode/httpcore/blob/1.0.9/httpcore/_backends/anyio.py
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import socket
import ssl

import anyio
import httpcore


@dataclass(slots=True, repr=False)
class NetworkIOObservation:
    """Original I/O facts from one exchange, before pool rethrows erase causes.

    Error links and Unix paths are protected diagnostics. Actual EOF means a
    nonzero stream read returned no bytes; it is not inferred from parser text.
    """

    stage: str | None = None
    error: BaseException | None = None
    cause: BaseException | None = None
    context: BaseException | None = None
    unix_path: str | None = None
    configured_unix_path: str | None = None
    eof: bool = False
    deadline_cancelled: bool = False


# silent-defaults: allow-contract -- Cancellation supplies no availability permission unless the caller proves a typed timeout's implementation cause.
def _availability(cause, remaining, observation, *, timeout_cancellation=False):
    if remaining == 0 or cause is None:
        return False
    explicit = observation.cause if cause is observation.error else cause.__cause__
    if type(cause) in (ExceptionGroup, BaseExceptionGroup):
        return all(_availability(child, remaining - 1, observation, timeout_cancellation=timeout_cancellation)
                   for child in cause.exceptions)
    if type(cause) is asyncio.CancelledError:
        return timeout_cancellation and (
            explicit is None or _availability(explicit, remaining - 1, observation, timeout_cancellation=True)
        )
    timeout = type(cause) in (TimeoutError, httpcore.ConnectTimeout, httpcore.ReadTimeout, httpcore.WriteTimeout)
    # Terminal native types do not borrow incidental handler context. Explicit
    # causes still matter: a timeout or EOF cannot hide an independent defect.
    # Every DNS errno describes resolver availability; exact ConnectionError is
    # the native connection family, not permission for arbitrary subclasses.
    if timeout or type(cause) in (
        socket.gaierror, ConnectionError, ConnectionRefusedError, ConnectionResetError,
        ConnectionAbortedError, BrokenPipeError, ssl.SSLEOFError, ssl.SSLZeroReturnError,
    ):
        return explicit is None or _availability(
            explicit, remaining - 1, observation, timeout_cancellation=timeout_cancellation or timeout
        )
    if isinstance(cause, ssl.SSLError):
        return False
    if type(cause) in (OSError, FileNotFoundError):
        if cause.errno is not None:
            available = cause.errno in {
                errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
                errno.ETIMEDOUT, errno.ENETDOWN, errno.ENETUNREACH,
                errno.EHOSTUNREACH, errno.EPIPE,
            } or (
                cause.errno == errno.ENOENT and observation.stage == "connect_unix"
                and observation.unix_path is not None
                and observation.unix_path == observation.configured_unix_path
            )
            return available and (explicit is None or _availability(
                explicit, remaining - 1, observation, timeout_cancellation=timeout_cancellation
            ))
    elif type(cause) not in (
        httpcore.ConnectError, httpcore.ReadError, httpcore.WriteError,
        anyio.BrokenResourceError, anyio.EndOfStream,
    ):
        return False
    context = observation.context if cause is observation.error else cause.__context__
    # AnyIO's grouped address failure uses an explicit OSError cause. An
    # errno-less local setup error cannot borrow incidental handler context.
    preceding = explicit if explicit is not None or type(cause) is OSError else context
    if preceding is not None:
        return _availability(preceding, remaining - 1, observation, timeout_cancellation=timeout_cancellation)
    return type(cause) in (anyio.BrokenResourceError, anyio.EndOfStream)


def network_availability(primary, observation, *, network_failure):
    """Classify captured I/O for API owners; local lookalikes grant no permission.

    Only known I/O wrappers may expose context erased by the pinned pool. A
    caller interruption cannot become an SDK-authored pending-I/O timeout.
    Status, environment, operation effects and retirement remain API decisions.
    """
    if type(observation) is not NetworkIOObservation:
        return False
    if observation.deadline_cancelled and type(primary) is TimeoutError:
        return (
            observation.error is network_failure and isinstance(observation.error, asyncio.CancelledError)
            and _availability(observation.error, 8, observation, timeout_cancellation=True)
        )
    if primary is not network_failure:
        return False
    if observation.error is not None:
        return primary is observation.error and _availability(primary, 8, observation)
    return observation.eof and type(primary) is httpcore.RemoteProtocolError


class OwnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Track every acquired stream, including incomplete TLS connections."""

    def __init__(self, *, proxy_socket=None):
        self._backend = httpcore.AnyIOBackend()
        self._streams = set()
        self._proxy_socket = proxy_socket
        self.observation = NetworkIOObservation(configured_unix_path=proxy_socket)
        self._pending_stage = None

    @contextmanager
    def io(self, stage, *, path=None):
        self._pending_stage = stage
        try:
            yield
        except BaseException as error:
            facts = self.observation
            if facts.error is None or isinstance(facts.error, asyncio.CancelledError) and not isinstance(error, asyncio.CancelledError):
                facts.stage = stage
                facts.unix_path = path
                facts.error = error
                facts.cause = error.__cause__
                facts.context = error.__context__
            raise
        finally:
            self._pending_stage = None

    def cancel_pending_for_deadline(self):
        if self._pending_stage is not None:
            self.observation.deadline_cancelled = True

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if self._proxy_socket is not None:
            # httpcore 1.0.9 passes this backend into its HTTPS tunnel but drops
            # the proxy's uds argument (_async/http_proxy.py:146-166). Its fixed
            # metadata host must never cause DNS, TCP or direct fallback here.
            return await self.connect_unix_socket(self._proxy_socket, timeout=timeout, socket_options=socket_options)
        with self.io("connect_tcp"):
            raw = await self._backend.connect_tcp(
                host=host, port=port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        return self._own(raw)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        with self.io("connect_unix", path=path):
            raw = await self._backend.connect_unix_socket(path=path, timeout=timeout, socket_options=socket_options)
        return self._own(raw)

    def _own(self, raw):
        stream = _OwnedStream(raw, self)
        self._streams.add(stream)
        return stream

    async def aclose(self):
        for stream in tuple(self._streams):
            await stream.aclose()


class _OwnedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, network):
        self._stream = stream
        self._network = network
        self._owners = network._streams

    async def read(self, max_bytes, timeout=None):
        with self._network.io("read"):
            data = await self._stream.read(max_bytes, timeout)
        if max_bytes > 0 and data == b"":
            facts = self._network.observation
            facts.eof = True
            if facts.error is None:
                facts.stage = "read"
        return data

    async def write(self, buffer, timeout=None):
        with self._network.io("write"):
            await self._stream.write(buffer, timeout)

    def get_extra_info(self, info):
        return self._stream.get_extra_info(info)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # Assignment happens only after a successful upgrade: cancellation must
        # leave the raw connection reachable by the final cleanup owner.
        with self._network.io("start_tls"):
            self._stream = await self._stream.start_tls(ssl_context=ssl_context, server_hostname=server_hostname, timeout=timeout)
        return self

    async def aclose(self):
        if self in self._owners:
            await self._stream.aclose()
            self._owners.remove(self)
