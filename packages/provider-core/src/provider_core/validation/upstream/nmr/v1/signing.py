"""Admit origins and exact signing inputs for the NMR v1 RFC 9421 profile.

Sources: NMR API 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
security/credential_refs.py and security/authentication/_rfc9421_profile.py.
Origin normalization and exact-target admission retain the supported SDK profile;
they grant neither network access nor permission to retry a signed request.
"""

from provider_core.container_guard import require_container

require_container()

import re
import reprlib
from ipaddress import IPv6Address, ip_address
from urllib.parse import urlsplit

from provider_core.type_requirements import require_type


# The selected signature profile's expiry offset from creation
# repo-check: allow-module-constant
SIGNATURE_LIFETIME_SECONDS = 300

# Visible ASCII bounds for origins and exact HTTP request-target bytes
# repo-check: allow-module-constant
MIN_VISIBLE_ASCII_CODEPOINT = 33
# repo-check: allow-module-constant
MAX_VISIBLE_ASCII_CODEPOINT = 126


def https_origin(api_origin):
    """Admit one HTTPS origin; never reinterpret credentials or a URL path."""

    require_type(api_origin, str)

    if not api_origin:
        raise ValueError(f"{api_origin=} should be nonempty")

    if not all(
        (
            (MIN_VISIBLE_ASCII_CODEPOINT <= (origin_codepoint := ord(character)) <= MAX_VISIBLE_ASCII_CODEPOINT)
            for character in api_origin
        ),
    ):
        raise ValueError(f"{origin_codepoint=} should be between {MIN_VISIBLE_ASCII_CODEPOINT=} and {MAX_VISIBLE_ASCII_CODEPOINT=}")

    try:
        parsed = urlsplit(api_origin)
        port = parsed.port
    except ValueError as cause:
        raise ValueError("Cannot parse the API origin") from cause

    host = parsed.hostname

    expected_scheme = "https"

    if not (parsed.scheme == expected_scheme):
        raise ValueError(f"{parsed.scheme=} should match {expected_scheme=}")

    if not host:
        raise ValueError(f"{host=} should be nonempty")

    # Empty userinfo components are present credentials too; do not print their contents
    if not (parsed.username is None):
        raise ValueError(f"{parsed.username is not None=} should be False")

    if not (parsed.password is None):
        raise ValueError(f"{parsed.password is not None=} should be False")

    expected_paths = ["", "/"]

    if not (parsed.path in expected_paths):
        raise ValueError(f"{reprlib.repr(parsed.path)=} should be one of {expected_paths=}")

    if not ("?" not in api_origin):
        raise ValueError(f"{'?' in api_origin=} should be False")

    if not ("#" not in api_origin):
        raise ValueError(f"{'#' in api_origin=} should be False")

    if parsed.netloc.endswith(":"):
        raise ValueError(f"{port=} should be set after an explicit ':'")

    if not ("%" not in host):
        raise ValueError(f"{'%' in host=} should be False")

    if (port is not None) and (port <= 0):
        raise ValueError(f"{port=} should be positive")

    try:
        address = ip_address(host)
    except ValueError as cause:
        # urlsplit also accepts bracketed IPvFuture literals. Treating one as a
        # DNS hostname would silently change the configured destination
        if parsed.netloc.startswith("["):
            raise ValueError(f"{reprlib.repr(host)=} should be an IPv6 address inside brackets") from cause

        expected_hostname_pattern = r"[A-Za-z0-9.-]+"

        if not re.fullmatch(expected_hostname_pattern, host):
            raise ValueError(f"{reprlib.repr(host)=} should match {expected_hostname_pattern=}") from None

        authority = host.lower()
    else:
        if isinstance(address, IPv6Address):
            authority = f"[{address}]"  # machine-string: HTTP authority syntax preserves the IPv6 address for request signing
        else:
            authority = str(address)

    if (port is not None) and (port != 443):
        authority += f":{port}"  # machine-string: HTTP authority syntax preserves the explicit port for request signing

    return ("https://" + authority, authority)


def require_credential_ref(credential_ref):
    """Admit the reference grammar shared by issued artifacts and signed requests."""

    require_type(credential_ref, str)

    credential_ref_character_count = len(credential_ref)
    max_credential_ref_characters = 128

    if not (credential_ref_character_count <= max_credential_ref_characters):
        raise ValueError(f"{credential_ref_character_count=} should be at most {max_credential_ref_characters=}")

    expected_credential_ref_pattern = r"credential:[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*"

    if not re.fullmatch(expected_credential_ref_pattern, credential_ref):
        raise ValueError(f"{reprlib.repr(credential_ref)=} should match {expected_credential_ref_pattern=}")


def request_target(method, path, query):
    """Reject ambiguous target forms before a client can normalize signed bytes."""

    require_type(method, str)

    if not re.fullmatch((expected_pattern := r"[A-Z]+"), method):
        raise ValueError(f"{method=} should match {expected_pattern=}")

    require_type(path, str)

    parsed_path = urlsplit(path).path

    if not (path.startswith("/") and (parsed_path == path)):
        raise ValueError(f"{path=} should be an absolute path without an authority, query or fragment")

    require_type(query, bytes)

    try:
        # silent-defaults: allow-contract -- An empty query omits the delimiter; a nonempty query retains its exact signed bytes.
        target = path.encode("ascii") + (b"?" + query if query else b"")
    except UnicodeError as cause:
        raise ValueError("Cannot encode path as ASCII") from cause

    # Spaces, controls and DEL cannot form a request-target token; fragments are excluded
    if not all(
        (
            (MIN_VISIBLE_ASCII_CODEPOINT <= (target_byte := candidate_byte) <= MAX_VISIBLE_ASCII_CODEPOINT)
            for candidate_byte in target
        ),
    ):
        raise ValueError(f"{target_byte=} should be between {MIN_VISIBLE_ASCII_CODEPOINT=} and {MAX_VISIBLE_ASCII_CODEPOINT=}")

    if not (b"#" not in query):
        raise ValueError(f"{b'#' in query=} should be False")

    return target


def require_signature_inputs(*, body, created, nonce):
    """Admit the body representation, structured-field time and nonce length."""

    if body is not None:
        require_type(body, bytes)

    require_type(created, int)

    if not (0 <= created):
        raise ValueError(f"{created=} should be nonnegative")

    max_structured_field_integer = 999999999999999

    if not (created <= (max_structured_field_integer - SIGNATURE_LIFETIME_SECONDS)):
        expires = created + SIGNATURE_LIFETIME_SECONDS

        raise ValueError(f"{expires=} should be at most {max_structured_field_integer=}")

    require_type(nonce, bytes)

    nonce_byte_length = len(nonce)
    expected_nonce_bytes = 16

    if not (nonce_byte_length == expected_nonce_bytes):
        raise ValueError(f"{nonce_byte_length=} should match {expected_nonce_bytes=}")
