"""Construct exact RFC 9421 requests without owning time, randomness or network I/O.

Protocol sources and the Magnet signing precedent are pinned in package README.
The API caller supplies a fresh timestamp and nonce for each network attempt.
"""

from provider_core.container_guard import require_container

require_container()

import re
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import IPv6Address, ip_address
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import ser


def _https_origin(value):
    """Admit one HTTPS origin; never reinterpret credentials or a URL path."""
    if type(value) is not str or not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError("Cannot configure signing: supply one ASCII HTTPS origin without whitespace")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Cannot configure signing: the HTTPS origin has an invalid host or port") from None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or "?" in value
        or "#" in value
        or parsed.netloc.endswith(":")
        or "%" in host
        or (port is not None and port <= 0)
    ):
        raise ValueError("Cannot configure signing: HTTPS origin must contain only a host and optional port")
    try:
        address = ip_address(host)
    except ValueError:
        # urlsplit also accepts bracketed IPvFuture literals. Treating one as a
        # DNS hostname would silently change the configured destination.
        if parsed.netloc.startswith("["):
            raise ValueError("Cannot configure signing: bracketed origins must contain an IPv6 address") from None
        if re.fullmatch(r"[A-Za-z0-9.-]+", host) is None:
            raise ValueError("Cannot configure signing: the origin host is not an ASCII hostname") from None
        authority = host.lower()
    else:
        authority = f"[{address}]" if isinstance(address, IPv6Address) else str(address)
    if port is not None and port != 443:
        authority += f":{port}"
    return "https://" + authority, authority


@dataclass(frozen=True, slots=True, repr=False)
class SigningIdentity:
    """One configured API origin and Ed25519 credential, independent of transport."""

    origin: str
    credential_ref: str
    private_key: Ed25519PrivateKey
    authority: str = field(init=False)

    def __post_init__(self):
        origin, authority = _https_origin(self.origin)
        require_credential_ref(self.credential_ref)
        if not isinstance(self.private_key, Ed25519PrivateKey):
            raise TypeError("Cannot configure signing: an Ed25519 private key is required")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "authority", authority)


def require_credential_ref(value):
    """Admit the reference grammar shared by issued artifacts and signed requests."""
    if (
        not (type(value) is str and len(value) <= 128 and re.fullmatch('credential:[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*', value) is not None)
    ):
        raise ValueError("Cannot configure signing: credential reference must use bounded credential:name segments")


def _request_target(method, path, query):
    """Reject ambiguous target forms before a client can normalize signed bytes."""
    if type(method) is not str or re.fullmatch(r"[A-Z]+", method) is None:
        raise ValueError("Cannot sign request: method must be an uppercase ASCII HTTP method")
    if type(path) is not str or not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ValueError("Cannot sign request: path must be absolute, with query supplied separately and no fragment")
    if type(query) is not bytes:
        raise TypeError("Cannot sign request: query must be immutable ASCII bytes")
    try:
        # silent-defaults: allow-contract -- An empty query omits the delimiter; a nonempty query retains its exact signed bytes.
        target = path.encode("ascii") + (b"?" + query if query else b"")
    except UnicodeError:
        raise ValueError("Cannot sign request: target must contain ASCII bytes only") from None
    if any(c <= 32 or c >= 127 for c in target) or b"#" in query:
        raise ValueError("Cannot sign request: target contains whitespace, control bytes or a fragment")
    return target


def signed_request(identity, *, method, path, query, body, created, nonce):
    """Sign the exact target and optional JSON bytes; reject HTTPX rewriting.

    None denotes no body. Empty bytes are a body and therefore require a digest.
    This function grants no replay policy: the operation owner retains body bytes
    and chooses whether another attempt is allowed.
    """
    target = _request_target(method, path, query)
    if body is not None and type(body) is not bytes:
        raise TypeError("Cannot sign request: body must be immutable bytes or None")
    if type(created) is not int or not 0 <= created <= 999999999999699:
        raise ValueError("Cannot sign request: timestamp and 300-second expiry must fit structured-field integers")
    if type(nonce) is not bytes or len(nonce) != 16:
        raise ValueError("Cannot sign request: nonce must contain exactly 16 random bytes")
    headers = {"Accept": "application/json, application/problem+json"}
    values = {"@method": method, "@authority": identity.authority, "@path": path, "@query": "?" + query.decode("ascii")}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Digest"] = ser({"sha-256": (sha256(body).digest(), {})})
        values["content-type"] = headers["Content-Type"]
        values["content-digest"] = headers["Content-Digest"]
    member = (
        [(name, {}) for name in values],
        {
            "created": created,
            "expires": created + 300,
            "nonce": urlsafe_b64encode(nonce).rstrip(b"=").decode("ascii"),
            "keyid": identity.credential_ref,
            "tag": "nmr-api-v1",
        },
    )
    base = "\n".join([f'"{name}": {value}' for name, value in values.items()] + [f'"@signature-params": {ser([member])}'])
    headers["Signature-Input"] = ser({"sig1": member})
    headers["Signature"] = ser({"sig1": (identity.private_key.sign(base.encode("ascii")), {})})
    request = httpx.Request(method, identity.origin + target.decode("ascii"), headers=headers, content=body)
    if request.url.raw_path != target or request.headers["Host"] != identity.authority:
        raise ValueError("Cannot sign request: HTTP client rewrote the target or authority")
    return request
