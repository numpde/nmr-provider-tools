"""Admit the NMR store's whole-file response and bearer header representation.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
upload_transfer_http.py::_read_response_start and upload_transfer_http_contract.py.
These checks grant no store-origin permission, transfer budget or file custody.
"""

from provider_core.container_guard import require_container

require_container()

import base64

from provider_core.response_json import ResponseRejected
from provider_core.http_response import HttpResponse, HttpResponseHead
from provider_core.validation.upstream.nmr.v1.upload_response import ReadGrant


def require_download_capability(capability: str) -> None:
    """Admit the capability's HTTP header representation immediately before use."""

    # Keep the bearer value in one visible-ASCII header token (33–126)
    if (not capability) or not all((32 < ord(character) < 127) for character in capability):
        raise ResponseRejected("download bearer cannot be represented as a single HTTP authorization value")


def require_store_headers(response: HttpResponse | HttpResponseHead, grant: ReadGrant) -> None:
    """Admit a complete whole-file head before receiving or charging body bytes."""

    if response.status != 200:
        raise ResponseRejected(f"store returned HTTP {response.status=}; no verified bytes were obtained")

    digest = base64.b64encode(bytes.fromhex(grant.content_hash.removeprefix("sha256:")))

    for (name, expected) in (
        (b"content-type", b"application/octet-stream"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(grant.byte_length).encode("ascii")),
        (b"content-digest", b"sha-256=:" + digest + b":"),
    ):
        if [value for (key, value) in response.headers if key.lower() == name] != [expected]:
            raise ResponseRejected(f"store response {name.decode()=} does not match the granted whole-file transfer")

    if any((name.lower() in {b"content-encoding", b"content-range"}) for (name, _) in response.headers):
        raise ResponseRejected("encoded or partial store responses are not admitted")
