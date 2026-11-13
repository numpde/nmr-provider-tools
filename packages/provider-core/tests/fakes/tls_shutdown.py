"""Exact HTTP wire replies with independently controlled TLS shutdown.

The ordinary asyncio TLS peer always performs a goodbye. This peer deliberately
closes its descriptor without one, after sending the selected wire transcript.
"""

import asyncio
from contextlib import AsyncExitStack
import os
import socket
import threading
from unittest.mock import patch

from anyio.streams.tls import TLSStream
from provider_core.network import OwnedNetworkBackend
from provider_core.transport import HttpTransport
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.unix_proxy import UnixProxy


class ShutdownPeer:
    # silent-defaults: allow-contract -- Ordinary probes send one write and immediately close; faults select fragmentation, a held peer or failed local close.
    def __init__(self, replies, *, tunnel, fragment_size=None, hold=False, raw_close_failure=None, hold_raw_close=False, raw_close_failures=()):
        self.replies = replies
        self.tunnel = tunnel
        self.fragment_size = fragment_size
        self.hold = hold
        self.release = threading.Event()
        self.requests = []
        self.connections_closed = 0
        self.raw_close_failure = raw_close_failure
        self.raw_close_failures = list(raw_close_failures)
        self.hold_raw_close = hold_raw_close
        self.raw_close_started = asyncio.Event()
        self.raw_close_release = asyncio.Event()
        self.raw_close_attempts = 0
        self.raw_close_successes = 0
        self.raw_sockets = []
        self.raw_closers = []
        self.shutdown_started = asyncio.Event()

    async def __aenter__(self):
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        certificate = await self.stack.enter_async_context(TlsPeer("success"))
        self.listener = self.stack.enter_context(socket.socket())
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(len(self.replies))
        self.listener.settimeout(0.1)
        self.origin = f"https://127.0.0.1:{self.listener.getsockname()[1]}"
        proxy_path = None
        if self.tunnel:
            proxy = await self.stack.enter_async_context(UnixProxy(self.origin, "tunnel"))
            proxy_path = proxy.path
        self.stack.enter_context(patch("provider_core.transport.OwnedNetworkBackend", self.backend))
        unwrap = TLSStream.unwrap

        async def observe_shutdown(stream):
            self.shutdown_started.set()
            return await unwrap(stream)

        self.stack.enter_context(patch.object(TLSStream, "unwrap", observe_shutdown))
        self.transport = HttpTransport(
            trust=certificate.trust, exchange_seconds=1, retirement_seconds=0.3,
            proxy_socket=proxy_path,
        )
        self.server = asyncio.create_task(asyncio.to_thread(self.serve, certificate.server_trust))
        return self

    def backend(self, **options):
        backend = OwnedNetworkBackend(**options)
        own = backend._own

        def observe(raw):
            close = raw.aclose
            self.raw_closers.append(close)
            self.raw_sockets.append(raw.get_extra_info("socket"))

            async def close_raw():
                self.raw_close_attempts += 1
                self.raw_close_started.set()
                while self.hold_raw_close and not self.raw_close_release.is_set():
                    try:
                        await self.raw_close_release.wait()
                    # silent-defaults: allow-contract -- This fault models a physical close that cannot finish until the test releases it, despite observer cancellation.
                    except asyncio.CancelledError:
                        pass
                if self.raw_close_failures:
                    raise self.raw_close_failures.pop(0)
                if self.raw_close_failure is not None:
                    raise self.raw_close_failure
                await close()
                self.raw_close_successes += 1

            raw.aclose = close_raw
            return own(raw)

        backend._own = observe
        return backend

    def serve(self, trust):
        for reply in self.replies:
            while not self.release.is_set():
                try:
                    raw, _address = self.listener.accept()
                    break
                except TimeoutError:
                    continue
            else:
                return
            with raw:
                raw.settimeout(3)
                with trust.wrap_socket(raw, server_side=True) as connection:
                    request = bytearray()
                    while not request.endswith(b"\r\n\r\n"):
                        chunk = connection.recv(1)
                        if not chunk or len(request) >= 16384:
                            raise AssertionError("TLS shutdown probe received an incomplete or oversized request")
                        request.extend(chunk)
                    self.requests.append(bytes(request))
                    step = len(reply) if self.fragment_size is None else self.fragment_size
                    for offset in range(0, len(reply), step):
                        connection.sendall(reply[offset:offset + step])
                    if self.hold and not self.release.wait(3):
                        raise AssertionError("TLS shutdown probe was not released during teardown")
                    # SSLSocket.close is deliberately bypassed: no TLS goodbye
                    # may accidentally turn a ragged EOF into a cooperative peer.
                    os.close(connection.detach())
                    self.connections_closed += 1

    async def __aexit__(self, kind, value, traceback):
        self.release.set()
        self.raw_close_release.set()
        try:
            if self.transport._active is not None:
                await asyncio.wait_for(asyncio.shield(self.transport._active.task), 4)
            await asyncio.wait_for(asyncio.shield(self.server), 4)
        finally:
            try:
                # Test faults may deny the application its close proof. The
                # fixture still owns the real descriptors after assertions.
                for close in self.raw_closers:
                    await close()
            finally:
                await self.stack.__aexit__(kind, value, traceback)


# silent-defaults: allow-contract -- The ordinary synthetic reply is HTTP 200 without additional headers; each scenario may select its status and headers.
def fixed_response(body, *, status=b"200 OK", headers=b""):
    return b"HTTP/1.1 " + status + b"\r\n" + headers + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
