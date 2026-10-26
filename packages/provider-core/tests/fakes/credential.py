"""Disposable API-shaped credentials, never usable against a deployed service.

Field authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/credential_registration.py::issue_provider_credential_artifacts.
"""

import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def document(*, key=None, **changes):
    key = Ed25519PrivateKey.generate() if key is None else key
    return {
        "schema_id": "nmr.provider.private_signing_credential.v1",
        "profile": "dev-local",
        "principal_ref": "provider:example",
        "credential_ref": "credential:example",
        "algorithm": "ed25519",
        "public_key_spki_der_b64": base64.b64encode(
            key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        ).decode(),
        "private_key_pkcs8_pem": key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
    } | changes


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
