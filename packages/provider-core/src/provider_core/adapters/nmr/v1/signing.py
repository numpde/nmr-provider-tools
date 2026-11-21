"""Construct exact RFC 9421 requests without owning time, randomness or network I/O.

Protocol sources and the Magnet signing precedent are pinned in package README.
The API caller supplies a fresh timestamp and nonce for each network attempt.
"""

from provider_core.container_guard import require_container

require_container()

from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from hashlib import sha256

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import ser

from provider_core.validation.upstream.nmr.v1.signing import (
    SIGNATURE_LIFETIME_SECONDS,
    https_origin,
    require_credential_ref,
    require_signature_inputs,
    request_target,
)

from provider_core.type_requirements import require_instance


@dataclass(frozen=True, slots=True, repr=False)
class SigningIdentity:
    """One configured API origin and Ed25519 credential, independent of transport."""

    origin: str
    credential_ref: str
    private_key: Ed25519PrivateKey
    authority: str = field(init=False)

    def __post_init__(self):
        (origin, authority) = https_origin(self.origin)

        require_credential_ref(self.credential_ref)

        actual_private_key = self.private_key

        require_instance(actual_private_key, Ed25519PrivateKey)

        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "authority", authority)


def signed_request(identity, *, method, path, query, body, created, nonce):
    """Sign the exact target and optional JSON bytes; reject HTTPX rewriting.

    None denotes no body. Empty bytes are a body and therefore require a digest.
    This function grants no replay policy: the operation owner retains body bytes
    and chooses whether another attempt is allowed.
    """
    target = request_target(method, path, query)

    require_signature_inputs(body=body, created=created, nonce=nonce)

    headers = {"Accept": "application/json, application/problem+json"}
    component_values = {"@method": method, "@authority": identity.authority, "@path": path, "@query": "?" + query.decode("ascii")}

    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Digest"] = ser({"sha-256": (sha256(body).digest(), {})})
        component_values["content-type"] = headers["Content-Type"]
        component_values["content-digest"] = headers["Content-Digest"]

    member = (
        [(component_name, {}) for component_name in component_values],
        {
            "created": created,
            "expires": created + SIGNATURE_LIFETIME_SECONDS,
            "nonce": urlsafe_b64encode(nonce).rstrip(b"=").decode("ascii"),
            "keyid": identity.credential_ref,
            "tag": "nmr-api-v1",
        },
    )

    header_lines = [
        f'"{component_name}": {component_value}'  # machine-string: RFC 9421 signature base preserves these exact component lines
        for (component_name, component_value) in component_values.items()
    ]

    signature_parameters = f'"@signature-params": {ser([member])}'  # machine-string: RFC 9421 signature base requires this exact serialization
    base = "\n".join(header_lines + [signature_parameters])
    headers["Signature-Input"] = ser({"sig1": member})
    headers["Signature"] = ser({"sig1": (identity.private_key.sign(base.encode("ascii")), {})})
    signed_http_request = httpx.Request(method, identity.origin + target.decode("ascii"), headers=headers, content=body)

    # HTTPX must retain the bytes we signed; this checks the constructed request
    constructed_target = signed_http_request.url.raw_path

    if not (constructed_target == target):
        # Queries can be long; show complete digests without dumping request parameters
        constructed_target_sha256 = (
            sha256(constructed_target)
            .hexdigest()
        )
        signed_target_sha256 = (
            sha256(target)
            .hexdigest()
        )

        raise ValueError(f"{constructed_target_sha256=} should match {signed_target_sha256=}")

    if not (signed_http_request.headers["Host"] == identity.authority):
        raise ValueError(f"{signed_http_request.headers['Host']=} should match {identity.authority=}")

    return signed_http_request
