"""Bound delivery IPC to one private Unix socket, reusing HTTP retirement ownership."""

from provider_core.container_guard import require_container

require_container()

import reprlib

import httpx

from provider_core.adapters import httpcore as backend_wire

from provider_core.transport import HttpTransport, require_unix_socket_path
from provider_core.tools import require_positive_number

# This synthetic HTTP authority names Unix IPC and never selects an external API.
# repo-check: allow-module-constant
DELIVERY_ORIGIN = "http://delivery.invalid"


class UnixHttpTransport(HttpTransport):
    """Plain local HTTP has no TLS identity, CONNECT proxy or TCP alternative.

    The URL authority is fixed protocol metadata. The configured socket grant
    selects the real recipient; delivery's client owns its small route vocabulary.
    """

    def __init__(self, *, socket_path, exchange_seconds, retirement_seconds):
        require_unix_socket_path(socket_path)
        self._exchange_seconds = require_positive_number(exchange_seconds)
        self._connect_seconds = None
        self._retirement_seconds = require_positive_number(retirement_seconds)
        self._socket_path = socket_path
        self._trust = self._proxy_socket = None
        self._active = None
        self.last_failure = None

    def _connection_pool(self, options):
        return backend_wire.unix_connection_pool(options, socket_path=self._socket_path)

    def _require_destination(self, http_request):
        expected_destination = httpx.URL(DELIVERY_ORIGIN)

        if not (http_request.url.scheme == expected_destination.scheme):
            raise ValueError(f"{http_request.url.scheme=} should match {expected_destination.scheme=}")

        if not (http_request.url.raw_host == expected_destination.raw_host):
            raise ValueError(f"{reprlib.repr(http_request.url.raw_host)=} should match {expected_destination.raw_host=}")

        if not (http_request.url.port is None):
            raise ValueError(f"{http_request.url.port=} should be None")

        # Reject userinfo without printing a password carried in the URL
        if not (http_request.url.username == ""):
            raise ValueError(f"{bool(http_request.url.username)=} should be False")

        if not (http_request.url.password == ""):
            raise ValueError(f"{bool(http_request.url.password)=} should be False")
