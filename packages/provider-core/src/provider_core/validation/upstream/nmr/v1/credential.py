"""Admit the original API-issued provider signing artifact without issuing keys.

Adapted from SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/credential.py:49-108. Authority is nmr-api-v1
6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8, provider/{credential_registration,
private_signing_v1}.py and security/private_signing_credential.py:52-123.
The port uses core's bounded JSON/ref owners and keeps original decode evidence
private; all seven artifact fields are text, so canonicalization needs no generic
JSON framework. File authority and provider/profile binding belong to startup.
"""

from provider_core.container_guard import require_container

require_container()

import base64
import json
from dataclasses import dataclass, field

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key, load_pem_private_key

from provider_core.validation.upstream.nmr.v1.attempt_request import require_provider_ref
from provider_core.response_json import ResponseRejected, read_document
from provider_core.type_requirements import require_instance, require_type
from provider_core.validation.upstream.nmr.v1.signing import require_credential_ref

# The issued credential profile has a closed field set.
# repo-check: allow-module-constant
_FIELDS = {
    "schema_id",
    "profile",
    "principal_ref",
    "credential_ref",
    "algorithm",
    "public_key_spki_der_b64",
    "private_key_pkcs8_pem",
}


class CredentialRejected(ValueError):
    """Safe admission reason with private original parser/key evidence in cause."""

    def __init__(self, reason, *, cause=None):
        super().__init__(f"Cannot load provider signing credential: {reason=}")
        self.cause = cause


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    profile: str
    provider_ref: str
    credential_ref: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)


def parse_provider_credential(raw, *, maximum_bytes):
    """Verify the issued artifact within the caller's local startup byte allowance."""

    try:
        credential_fields = read_document(raw, maximum_bytes)
    except ResponseRejected as cause:
        raise CredentialRejected(f"supply the original API-issued JSON file within {maximum_bytes=} bytes.", cause=cause) from None

    require_type(credential_fields, dict, error_factory=CredentialRejected)

    if not (set(credential_fields) == _FIELDS):
        raise CredentialRejected("the seven API-issued fields must be present and contain text.")

    for (field_name, credential_value) in credential_fields.items():
        require_type(
            credential_value,
            str,
            error_factory=lambda diagnostic: CredentialRejected(f"{field_name=}; " + diagnostic),
        )

    try:
        canonical = (
            json.dumps(credential_fields, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
    except UnicodeError as cause:
        raise CredentialRejected("artifact text must be valid UTF-8.", cause=cause) from None

    if not (raw == canonical):
        raise CredentialRejected("use the original canonical JSON document followed by one newline.")

    if not (
        (credential_fields["schema_id"] == "nmr.provider.private_signing_credential.v1") and
        (credential_fields["algorithm"] == "ed25519") and
        (credential_fields["profile"] in ("dev-local", "dev", "run"))
    ):
        raise CredentialRejected("require the provider v1 artifact, Ed25519 and profile dev-local, dev or run.")

    try:
        require_provider_ref(credential_fields["principal_ref"])
        require_credential_ref(credential_fields["credential_ref"])
    except ValueError as cause:
        raise CredentialRejected(
            "principal_ref must identify a provider and credential_ref a signing credential.",
            cause=cause
        ) from None

    private_key = _signing_key(credential_fields)

    return ProviderCredential(credential_fields["profile"], credential_fields["principal_ref"], credential_fields["credential_ref"], private_key)


def _signing_key(credential_fields):
    try:
        public_der = base64.b64decode(credential_fields["public_key_spki_der_b64"], validate=True)

        if not (base64.b64encode(public_der).decode("ascii") == credential_fields["public_key_spki_der_b64"]):
            raise ValueError("Public key base64 is not canonical.")

        public_key = load_der_public_key(public_der)
        private_key = load_pem_private_key(credential_fields["private_key_pkcs8_pem"].encode("ascii"), password=None)
    except (TypeError, ValueError, UnicodeError, UnsupportedAlgorithm) as cause:
        raise CredentialRejected("decode a base64 DER public key and an unencrypted PEM private key.", cause=cause) from None

    require_instance(public_key, Ed25519PublicKey, error_factory=CredentialRejected)

    require_instance(private_key, Ed25519PrivateKey, error_factory=CredentialRejected)

    if not (private_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo) == public_der):
        raise CredentialRejected("public and private keys do not match.")

    return private_key
