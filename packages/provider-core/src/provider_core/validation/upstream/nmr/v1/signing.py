"""Admit origins and exact signing inputs for the NMR v1 RFC 9421 profile.

Sources: NMR API 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
security/credential_refs.py and security/authentication/_rfc9421_profile.py.
Origin normalization and exact-target admission retain the supported SDK profile;
they grant neither network access nor permission to retry a signed request.
"""

from provider_core.container_guard import require_container

require_container()

import re
from ipaddress import IPv6Address, ip_address
from urllib.parse import urlsplit

from provider_core.type_requirements import require_type


# The selected signature profile's expiry offset from creation
# repo-check: allow-module-constant
SIGNATURE_LIFETIME_SECONDS = 300


def https_origin(value):
    """Admit one HTTPS origin; never reinterpret credentials or a URL path."""

    require_type(value, str)

    if (not (value)) or (any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise ValueError("Cannot configure signing: supply one ASCII HTTPS origin without whitespace")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Cannot configure signing: the HTTPS origin has an invalid host or port") from None

    host = parsed.hostname

    if (
        (parsed.scheme != "https") or
        (not host) or
        (parsed.username is not None) or
        (parsed.password is not None) or
        (parsed.path not in {"", "/"}) or
        ("?" in value) or
        ("#" in value) or
        parsed.netloc.endswith(":") or
        ("%" in host) or
        ((port is not None) and (port <= 0))
    ):
        raise ValueError("Cannot configure signing: HTTPS origin must contain only a host and optional port")

    try:
        address = ip_address(host)
    except ValueError:
        # urlsplit also accepts bracketed IPvFuture literals. Treating one as a
        # DNS hostname would silently change the configured destination
        if parsed.netloc.startswith("["):
            raise ValueError("Cannot configure signing: bracketed origins must contain an IPv6 address") from None

        if re.fullmatch(r"[A-Za-z0-9.-]+", host) is None:
            raise ValueError("Cannot configure signing: the origin host is not an ASCII hostname") from None

        authority = host.lower()
    else:
        authority = f"[{address}]" if isinstance(address, IPv6Address) else str(address)

    if (port is not None) and (port != 443):
        authority += f":{port}"

    return ("https://" + authority, authority)


def require_credential_ref(value):
    """Admit the reference grammar shared by issued artifacts and signed requests."""

    require_type(value, str)

    if not ((len(value) <= 128) and (re.fullmatch('credential:[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*', value) is not None)):
        raise ValueError("Cannot configure signing: credential reference must use bounded credential:name segments")


def request_target(method, path, query):
    """Reject ambiguous target forms before a client can normalize signed bytes."""

    require_type(method, str)

    if not re.fullmatch((expected_pattern := r"[A-Z]+"), method):
        raise ValueError(f"{method=} should match {expected_pattern=}")

    require_type(path, str)

    parsed_path = urlsplit(path).path

    if not (path.startswith("/") and (parsed_path == path)):
        raise ValueError(f"{path=} should be an absolute path without an authority, query or fragment")

    if not (type(query) is (expected_type := bytes)):
        raise TypeError(f"{type(query)=} ≠ {expected_type=}")

    try:
        # silent-defaults: allow-contract -- An empty query omits the delimiter; a nonempty query retains its exact signed bytes.
        target = path.encode("ascii") + (b"?" + query if query else b"")
    except UnicodeError:
        raise ValueError("Cannot sign request: target must contain ASCII bytes only") from None

    if any(c <= 32 or c >= 127 for c in target) or b"#" in query:
        raise ValueError("Cannot sign request: target contains whitespace, control bytes or a fragment")

    return target


def require_signature_inputs(*, body, created, nonce):
    """Admit the body representation, structured-field time and nonce length."""

    if (body is not None) and (type(body) is not (expected_body_type := bytes)):
        raise TypeError(f"{type(body)=} ≠ {expected_body_type=}")

    require_type(created, int)

    if not (0 <= created):
        raise ValueError(f"{created=} should be nonnegative")

    max_structured_field_integer = 999999999999999

    if not (created <= max_structured_field_integer - SIGNATURE_LIFETIME_SECONDS):
        expires = created + SIGNATURE_LIFETIME_SECONDS

        raise ValueError(f"{expires=} should be at most {max_structured_field_integer=}")

    require_type(nonce, bytes)

    if len(nonce) != 16:
        raise ValueError("Cannot sign request: nonce must contain exactly 16 random bytes")
