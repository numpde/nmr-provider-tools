"""Admit the HTTP/1 request targets supported by the acquisition proxy."""

from provider_core.container_guard import require_container

require_container()

import reprlib
from urllib.parse import SplitResult

# Retain the proxy's admitted TCP port range for both request-target forms
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L41
# repo-check: allow-module-constant
(MIN_PORT, MAX_PORT) = (1, 65535)


def header_end(received: bytes | bytearray) -> int | None:
    """Return the byte after CRLFCRLF, leaving any coalesced body outside headers."""

    terminator = received.find(
        b"\r\n"
        b"\r\n",
    )

    return terminator + 4 if terminator >= 0 else None


def _require_port(port: int) -> int:
    """Reject explicit zero rather than selecting an omitted-port default."""

    if not (MIN_PORT <= port <= MAX_PORT):
        raise ValueError(f"{reprlib.repr(port)=} should be between {MIN_PORT=} and {MAX_PORT=}")

    return port


def read_connect_authority(target: str) -> tuple[str, int]:
    """Read the existing CONNECT host:port form without imposing local grants."""

    (host, separator, port_text) = target.rpartition(":")

    if not (separator and host):
        raise ValueError(f"{reprlib.repr(target)=} should be host:port")

    return (host, read_port(port_text))


def read_port(port_text: str) -> int:
    """Read the TCP port shared by request targets and configured authorities."""

    try:
        port = int(port_text)
    except ValueError as cause:
        raise ValueError(f"{reprlib.repr(port_text)=} should encode an int between {MIN_PORT=} and {MAX_PORT=}") from cause

    return _require_port(port)


def read_absolute_http_port(target: SplitResult) -> int:
    """Admit the parsed absolute HTTP URL and return its explicit or default port."""

    expected_schemes = ("http", "https")

    if not (target.scheme in expected_schemes):
        raise ValueError(f"{reprlib.repr(target.scheme)=} should be one of {expected_schemes=}")

    if not target.hostname:
        raise ValueError(f"{target.hostname=} should be nonempty")

    try:
        port = target.port
    except ValueError as cause:
        # urllib rejects malformed and out-of-range ports before returning a number
        port_text = target.netloc.rpartition(":")[2]

        raise ValueError(f"{reprlib.repr(port_text)=} should encode an int between {MIN_PORT=} and {MAX_PORT=}") from cause

    if port is None:
        # RFC 9110 sections 4.2.1 and 4.2.2 define omitted HTTP/HTTPS ports
        # https://www.rfc-editor.org/rfc/rfc9110.html#section-4.2.1
        # silent-defaults: allow-contract -- Omitted URI ports select HTTP 80 or HTTPS 443.
        return 443 if target.scheme == "https" else 80

    return _require_port(port)
