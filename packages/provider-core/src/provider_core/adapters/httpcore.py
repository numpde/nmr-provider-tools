"""Map request, pool and trace facts for the pinned httpcore 1.0.9 backend.

Sources: httpcore/_models.py::Request, _async/http11.py trace events and
_async/http_proxy.py::AsyncTunnelHTTPConnection. The caller owns pool options,
deadlines, network/file effects and retirement; this adapter owns none of them.
"""

from provider_core.container_guard import require_container

require_container()

import httpcore

from provider_core.http_response import HttpResponseHead


def _pool_options(options: dict) -> dict:
    backend_options = dict(options)
    backend_options["ssl_context"] = backend_options.pop("trust")

    return backend_options


def connection_pool(options: dict, *, via_unix_proxy: bool) -> httpcore.AsyncConnectionPool:
    """Construct the selected pool without changing the caller's options."""

    if via_unix_proxy:
        return httpcore.AsyncHTTPProxy(proxy_url="http://unix-proxy.invalid", **_pool_options(options))

    return httpcore.AsyncConnectionPool(**_pool_options(options))


def unix_connection_pool(options: dict, *, socket_path: str) -> httpcore.AsyncConnectionPool:
    """Bind local HTTP to its granted Unix socket without a TCP alternative."""

    return httpcore.AsyncConnectionPool(uds=socket_path, **_pool_options(options))


def require_connect_host(proxy_destination_host: bytes) -> None:
    """Refuse the pinned encoder's unsupported IPv6 CONNECT authority."""

    if not (b":" not in proxy_destination_host):
        raise ValueError(
            f"{proxy_destination_host=} should be a DNS name or IPv4 address; "
            f"httpcore 1.0.9 omits brackets for IPv6 literal destinations in CONNECT"
        )


def request_for_backend(request, *, io_timeout_seconds: float, connect_timeout_seconds: float, trace) -> httpcore.Request:
    """Preserve exact target, headers, body and caller-owned trace extensions."""

    timeouts = dict.fromkeys(("connect", "read", "write", "pool"), io_timeout_seconds)
    timeouts["connect"] = connect_timeout_seconds

    return httpcore.Request(
        method=request.method,
        url=httpcore.URL(
            scheme=request.url.raw_scheme,
            host=request.url.raw_host,
            port=request.url.port,
            target=request.url.raw_path,
        ),
        headers=request.headers.raw,
        content=request.content,
        extensions={"timeout": timeouts, "trace": trace},
    )


def origin_headers_started(event_name: str, info: dict, request: httpcore.Request) -> bool:
    """Distinguish the original request's send event from synthesized CONNECT."""

    return event_name == "http11.send_request_headers.started" and info["request"] is request


def received_header_status(event_name: str, info: dict) -> int | None:
    """Read a response-header trace status without retaining any event history."""

    if event_name == "http11.receive_response_headers.complete":
        return info["return_value"][1]

    # silent-defaults: allow-contract -- Other trace events supply no received status.
    return None


def response_head(response: httpcore.Response) -> HttpResponseHead:
    """Capture the original response metadata before body admission or decoding."""

    return HttpResponseHead(response.status, tuple(response.headers))
