"""Admit the NMR store's whole-file response and bearer header representation.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
upload_transfer_http.py::_read_response_start and upload_transfer_http_contract.py.
These checks grant no store-origin permission, transfer budget or file custody.
"""

from provider_core.container_guard import require_container

require_container()

import base64
import reprlib

from provider_core.response_json import ResponseRejected
from provider_core.http_response import HttpResponse, HttpResponseHead
from provider_core.validation.upstream.nmr.v1.upload_response import ReadGrant


def require_download_capability(capability: str) -> None:
    """Admit the capability's HTTP header representation immediately before use."""

    # Keep the bearer value in one visible-ASCII header token (33–126)
    # repo-check: allow-literal-guard-error -- The bearer is a live download credential; report its header constraint without rendering it
    if (not capability) or not all((32 < ord(character) < 127) for character in capability):
        raise ResponseRejected("Download bearer should be nonempty visible ASCII")


def require_store_headers(response: HttpResponse | HttpResponseHead, grant: ReadGrant) -> None:
    """Admit a complete whole-file head before receiving or charging body bytes."""

    if not (response.status == (expected_status := 200)):
        raise ResponseRejected(f"{response.status=} should match {expected_status=}")

    digest = base64.b64encode(bytes.fromhex(grant.content_hash.removeprefix("sha256:")))

    for (header_name, expected_header_value) in (
        (b"content-type", b"application/octet-stream"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(grant.byte_length).encode("ascii")),
        (b"content-digest", b"sha-256=:" + digest + b":"),
    ):
        header_values = [header_value for (response_header_name, header_value) in response.headers if response_header_name.lower() == header_name]

        if not (header_values == [expected_header_value]):
            raise ResponseRejected(f"{header_name=}; {reprlib.repr(header_values)=} should match {[expected_header_value]=}")

    prohibited_header_name = next(
        (
            response_header_name for (response_header_name, _) in response.headers
            if response_header_name.lower() in {b"content-encoding", b"content-range"}
        ),
        None,
    )

    if not (prohibited_header_name is None):
        raise ResponseRejected(f"{prohibited_header_name=} should be absent")
