"""API-marked local TLS replies; model/store peers retain their generic owner."""

import hashlib
from urllib.parse import urlsplit

from http_sf import parse
from tests.fakes.api import response_headers
from tests.fakes.tls_peer import TlsPeer


def api_tls_peer(mode, **options):
    if "response_headers" not in options:
        options["response_headers"] = response_headers()
    return TlsPeer(mode, **options)


def verify_signed_request(request, body, *, origin, key):
    """Verify the local peer's exact request, digest and signing-key binding."""
    method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
    headers = {
        name.lower(): value.strip()
        for line in request.split(b"\r\n")[1:]
        if b":" in line
        for name, value in [line.split(b":", 1)]
    }
    parsed = urlsplit(target)
    lines = [
        b'"@method": ' + method.encode(),
        b'"@authority": ' + origin.removeprefix("https://").encode(),
        b'"@path": ' + parsed.path.encode(),
        b'"@query": ?' + parsed.query.encode(),
    ]
    # None identifies the API's bodyless read-capability POST. Other mutation
    # routes carry JSON, including an empty body if explicitly supplied.
    if method in {"POST", "PUT"} and body is not None:
        if parse(headers[b"content-digest"], tltype="dictionary")["sha-256"][0] != hashlib.sha256(body).digest():
            raise AssertionError("Signed request digest differs from its exact body")
        lines += [b'"content-type": application/json', b'"content-digest": ' + headers[b"content-digest"]]
    elif body not in (None, b"") or b"content-type" in headers or b"content-digest" in headers:
        raise AssertionError("Bodyless signed request unexpectedly carries body content or its headers")
    lines.append(b'"@signature-params": ' + headers[b"signature-input"].split(b"=", 1)[1])
    signature = parse(headers[b"signature"], tltype="dictionary")["sig1"][0]
    key.public_key().verify(signature, b"\n".join(lines))
