"""Translate admitted proxy requests and encode HTTP/1 forwarding replies."""

from provider_core.container_guard import require_container

require_container()

from http import HTTPStatus
from urllib.parse import urlsplit

from provider_core.validation.upstream.http_proxy import read_absolute_http_port, read_connect_authority

# RFC 9110 section 9.3.6: the successful CONNECT response starts the tunnel
# https://www.rfc-editor.org/rfc/rfc9110.html#section-9.3.6
# repo-check: allow-module-constant
CONNECT_ESTABLISHED_RESPONSE = (
    b"HTTP/1.1 200 Connection Established\r\n"
    b"\r\n"
)


def closing_response(status: HTTPStatus) -> bytes:
    """Encode the selected empty response with an explicit connection close."""

    return (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        f"Connection: close\r\n"
        f"\r\n"  # machine-string: HTTP clients parse this complete status response; debug labels would corrupt the wire format
    ).encode("ascii")


def request_line(header: bytes) -> tuple[str, str, str]:
    """Read the existing space-delimited method, target and unmodified version."""

    first_line = header.split(b"\r\n", 1)[0]
    (method, target, version) = first_line.decode("ascii").split(" ", 2)

    return (method.upper(), target, version)


def parse_destination(header: bytes) -> tuple[str, str, int, bytes]:
    """Read the destination and preserve CONNECT or forward an origin-form target.

    This performs no I/O. The caller owns destination grants and the point at
    which forwarding starts; invalid targets raise ValueError before that point.
    """

    lines = header.split(b"\r\n")
    (method, target, version) = request_line(header)

    if method == "CONNECT":
        (host, port) = read_connect_authority(target)

        return (method, host, port, header)

    parsed = urlsplit(target)
    port = read_absolute_http_port(parsed)
    host = parsed.hostname

    # RFC 9112 section 3.2.1: forward the path and query, not the absolute URI
    # https://www.rfc-editor.org/rfc/rfc9112.html#section-3.2.1
    # silent-defaults: allow-contract -- Origin-form requires / for an empty URI path.
    origin_form_target = parsed.path or "/"

    if parsed.query:
        origin_form_target += "?" + parsed.query

    forwarded_request_line = f"{method} {origin_form_target} {version}"  # machine-string: the proxy peer parses this exact HTTP request line
    lines[0] = forwarded_request_line.encode("ascii")

    return (method, host, port, b"\r\n".join(lines))
