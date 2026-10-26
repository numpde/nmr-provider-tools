"""Retain TCP streams across TLS upgrade until their closure succeeds.

httpcore 1.0.9's AnyIOStream.start_tls catches Exception, not cancellation;
its connection object does not retain the raw stream when TLS is interrupted. Own the
raw stream through the public backend interface to close that gap. Source:
https://github.com/encode/httpcore/blob/1.0.9/httpcore/_backends/anyio.py
"""

from provider_core.container_guard import require_container

require_container()

import httpcore


class OwnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Track every acquired stream, including incomplete TLS connections."""

    def __init__(self, *, proxy_socket=None):
        self._backend = httpcore.AnyIOBackend()
        self._streams = set()
        self._proxy_socket = proxy_socket

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if self._proxy_socket is not None:
            # httpcore 1.0.9 passes this backend into its HTTPS tunnel but drops
            # the proxy's uds argument (_async/http_proxy.py:146-166). Its fixed
            # metadata host must never cause DNS, TCP or direct fallback here.
            return await self.connect_unix_socket(self._proxy_socket, timeout=timeout, socket_options=socket_options)
        raw = await self._backend.connect_tcp(
            host=host, port=port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        return self._own(raw)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raw = await self._backend.connect_unix_socket(path=path, timeout=timeout, socket_options=socket_options)
        return self._own(raw)

    def _own(self, raw):
        stream = _OwnedStream(raw, self._streams)
        self._streams.add(stream)
        return stream

    async def aclose(self):
        for stream in tuple(self._streams):
            await stream.aclose()


class _OwnedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, owners):
        self._stream = stream
        self._owners = owners

    async def read(self, max_bytes, timeout=None):
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        await self._stream.write(buffer, timeout)

    def get_extra_info(self, info):
        return self._stream.get_extra_info(info)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # Assignment happens only after a successful upgrade: cancellation must
        # leave the raw connection reachable by the final cleanup owner.
        self._stream = await self._stream.start_tls(ssl_context=ssl_context, server_hostname=server_hostname, timeout=timeout)
        return self

    async def aclose(self):
        if self in self._owners:
            await self._stream.aclose()
            self._owners.remove(self)
