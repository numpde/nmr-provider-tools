"""Immutable received HTTP facts without transport effects or service admission."""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass, field


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
    """Complete response bytes with original headers and no status interpretation.

    exchange returns raw bytes; exchange_decoded returns bounded decoded bytes.
    Content-Encoding and Content-Length still describe the received wire entity.
    """

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
