"""Bound delivery IPC to one private Unix socket, reusing HTTP retirement ownership."""

from provider_core.container_guard import require_container

require_container()

import httpcore

from provider_core.transport import HttpTransport, _positive_seconds, require_unix_socket_path

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
        self._exchange_seconds = _positive_seconds(exchange_seconds)
        self._retirement_seconds = _positive_seconds(retirement_seconds)
        self._socket_path = socket_path
        self._trust = self._proxy_socket = None
        self._active = None
        self.last_failure = None

    def _connection_pool(self, options):
        return httpcore.AsyncConnectionPool(uds=self._socket_path, **options)

    def _require_destination(self, request):
        if (request.url.scheme != "http" or request.url.raw_host != b"delivery.invalid"
                or request.url.port is not None or request.url.username or request.url.password):
            raise ValueError("Delivery HTTP requests must use the fixed local origin; only the configured Unix socket selects a destination.")
