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

from provider_core.attempt_request import require_provider_ref
from provider_core.response_json import ResponseRejected, read_document
from provider_core.signing import require_credential_ref

# The issued credential artifact has a bounded startup envelope.
# repo-check: allow-module-constant
MAX_CREDENTIAL_BYTES = 16384
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

    def __init__(self, message, *, cause=None):
        super().__init__(f"Cannot load provider signing credential: {message}")
        self.cause = cause


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    profile: str
    provider_ref: str
    credential_ref: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)


def parse_provider_credential(raw):
    """Verify the closed issued document and return its identity/signing capability."""
    try:
        document = read_document(raw, MAX_CREDENTIAL_BYTES)
    except ResponseRejected as cause:
        raise CredentialRejected("supply the original API-issued JSON file within 16384 bytes.", cause=cause) from None
    if type(document) is not dict or set(document) != _FIELDS or any(type(value) is not str for value in document.values()):
        raise CredentialRejected("the seven API-issued fields must be present and contain text.")
    try:
        canonical = (
            json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
    except UnicodeError as cause:
        raise CredentialRejected("artifact text must be valid UTF-8.", cause=cause) from None
    if raw != canonical:
        raise CredentialRejected("use the original canonical JSON document followed by one newline.")
    if (
        document["schema_id"] != "nmr.provider.private_signing_credential.v1"
        or document["algorithm"] != "ed25519"
        or document["profile"] not in ("dev-local", "dev", "run")
    ):
        raise CredentialRejected("require the provider v1 artifact, Ed25519 and profile dev-local, dev or run.")
    try:
        require_provider_ref(document["principal_ref"])
        require_credential_ref(document["credential_ref"])
    except ValueError as cause:
        raise CredentialRejected(
            "principal_ref must identify a provider and credential_ref a signing credential.", cause=cause
        ) from None
    private_key = _signing_key(document)
    return ProviderCredential(document["profile"], document["principal_ref"], document["credential_ref"], private_key)


def _signing_key(document):
    try:
        public_der = base64.b64decode(document["public_key_spki_der_b64"], validate=True)
        if base64.b64encode(public_der).decode("ascii") != document["public_key_spki_der_b64"]:
            raise ValueError("Public key base64 is not canonical.")
        public_key = load_der_public_key(public_der)
        private_key = load_pem_private_key(document["private_key_pkcs8_pem"].encode("ascii"), password=None)
    except (TypeError, ValueError, UnicodeError, UnsupportedAlgorithm) as cause:
        raise CredentialRejected("decode a base64 DER public key and an unencrypted PEM private key.", cause=cause) from None
    if not isinstance(public_key, Ed25519PublicKey) or not isinstance(private_key, Ed25519PrivateKey):
        raise CredentialRejected("both keys must use Ed25519.")
    if private_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo) != public_der:
        raise CredentialRejected("public and private keys do not match.")
    return private_key
