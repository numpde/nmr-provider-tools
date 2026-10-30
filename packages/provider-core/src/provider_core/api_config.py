"""Admit existing API signing and TLS settings without provider-specific imports.

Moves nmrnet-repro 29fb2f45d5a987b43767f782f88748dbeddc30c5,
nmrnet_provider/config.py::ApiSettings/HttpsSettings/_api/_https/_api_fields.
Provider configuration retains its own envelope and scientific settings.
"""

from provider_core.container_guard import require_container

require_container()

import os
import ssl
from dataclasses import dataclass

from provider_core.api import require_api_topology
from provider_core.credential import MAX_CREDENTIAL_BYTES, parse_provider_credential
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport, require_transport_settings


class ApiConfigurationRejected(ValueError):
    """Safe startup reason, with original trust-loading evidence kept protected."""

    # Closed-field or credential-binding refusal has no native cause; trust material admission can retain one.
    def __init__(self, message, *, cause=None):
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True, slots=True, repr=False)
class HttpsSettings:
    trust: ssl.SSLContext
    exchange_seconds: float
    retirement_seconds: float
    proxy_socket: str | None = None

    def __post_init__(self):
        require_transport_settings(trust=self.trust, exchange_seconds=self.exchange_seconds,
                                   retirement_seconds=self.retirement_seconds, proxy_socket=self.proxy_socket)

    def new_transport(self):
        """Give each runtime role a fresh, independently retired exchange owner."""
        return HttpTransport(trust=self.trust, exchange_seconds=self.exchange_seconds,
                             retirement_seconds=self.retirement_seconds, proxy_socket=self.proxy_socket)


@dataclass(frozen=True, slots=True, repr=False)
class ApiSettings:
    identity: SigningIdentity
    http: HttpsSettings
    expected_topology: str

    def __post_init__(self):
        require_api_topology(self.expected_topology)


@dataclass(frozen=True, slots=True, repr=False)
class ApiPublicationMaterial:
    """Original provider publication inputs retained independently of file paths."""

    origin: str
    profile: str
    topology: str
    provider_ref: str
    exchange_seconds: float
    retirement_seconds: float
    credential_bytes: bytes
    ca_bytes: bytes | None

    def api_fields(self):
        return {"origin": self.origin, "profile": self.profile, "topology": self.topology,
                "provider_ref": self.provider_ref, "exchange_seconds": self.exchange_seconds,
                "retirement_seconds": self.retirement_seconds}


def _require_explicit_tls_environment():
    # CPython v3.12.12 Lib/ssl.py::create_default_context honors SSLKEYLOGFILE;
    # OpenSSL root overrides also require explicit startup grants.
    if any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE")):
        raise ApiConfigurationRejected("remove ambient SSL_CERT_FILE, SSL_CERT_DIR and SSLKEYLOGFILE; configure each explicit ca_file instead.")


def _https_from_material(table, label, raw):
    if raw is not None:
        try:
            pem = raw.decode("ascii")
        except UnicodeDecodeError as cause:
            raise ApiConfigurationRejected(f"Cannot load {label} CA: the PEM certificate text must be ASCII.", cause=cause) from None
        try:
            trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            trust.load_verify_locations(cadata=pem)
        except (ValueError, OSError) as cause:
            raise ApiConfigurationRejected(f"Cannot configure {label} CA trust: OpenSSL could not load the PEM certificate data.", cause=cause) from None
    else:
        try:
            trust = ssl.create_default_context()
        except (OSError, ssl.SSLError) as cause:
            raise ApiConfigurationRejected(f"{label} system TLS roots could not be loaded.", cause=cause) from None
    return HttpsSettings(trust, table["exchange_seconds"], table["retirement_seconds"])


def _read_https_settings(table, label, read_file):
    _require_explicit_tls_environment()
    raw = None
    if "ca_file" in table:
        raw = read_file(table["ca_file"], maximum_bytes=1048576, label=label + " CA")
        if type(raw) is not bytes:
            raise ApiConfigurationRejected(f"Cannot load {label} CA: the selected reader did not return bytes.")
    return _https_from_material(table, label, raw), raw


def https_settings(table, label, read_file):
    """Load explicit CA bytes once; ambient environment never grants TLS roots."""
    settings, _ = _read_https_settings(table, label, read_file)
    return settings


def require_api_fields(table):
    required = {"origin", "profile", "topology", "provider_ref", "credential_file", "exchange_seconds", "retirement_seconds"}
    if type(table) is not dict or not required <= table.keys() or table.keys() - required - {"ca_file"}:
        raise ApiConfigurationRejected("API must contain exactly its documented fields.")


def _signing_identity(table, credential_bytes):
    credential = parse_provider_credential(credential_bytes)
    if (credential.profile, credential.provider_ref) != (table["profile"], table["provider_ref"]):
        raise ApiConfigurationRejected("API signing credential does not match the configured profile and provider.")
    identity = SigningIdentity(table["origin"], credential.credential_ref, credential.private_key)
    return identity, credential.provider_ref


def api_settings(table, read_file):
    """Bind the verified credential principal/profile before constructing signing."""
    settings, provider_ref, _ = api_settings_with_material(table, read_file)
    return settings, provider_ref


def api_settings_with_material(table, read_file):
    """Admit one API owner and retain the exact inputs needed for its outbox."""
    require_api_fields(table)
    raw = read_file(table["credential_file"], maximum_bytes=MAX_CREDENTIAL_BYTES, label="API signing credential", secret=True)
    identity, provider_ref = _signing_identity(table, raw)
    http, ca_bytes = _read_https_settings(table, "API", read_file)
    settings = ApiSettings(identity, http, table["topology"])
    material = ApiPublicationMaterial(identity.origin, table["profile"], table["topology"], provider_ref,
                                      table["exchange_seconds"], table["retirement_seconds"], raw, ca_bytes)
    return settings, provider_ref, material


def api_settings_from_material(table, *, credential_bytes, ca_bytes=None):
    """Construct the API owner from retained credential and optional CA bytes."""
    required = {"origin", "profile", "topology", "provider_ref", "exchange_seconds", "retirement_seconds"}
    if type(table) is not dict or set(table) != required:
        raise ApiConfigurationRejected("API publication settings must contain the documented non-file fields.")
    if type(credential_bytes) is not bytes or len(credential_bytes) > MAX_CREDENTIAL_BYTES:
        raise ApiConfigurationRejected("API signing credential requires bounded issued-document bytes.")
    identity, provider_ref = _signing_identity(table, credential_bytes)
    _require_explicit_tls_environment()
    if ca_bytes is not None and (type(ca_bytes) is not bytes or len(ca_bytes) > 1048576):
        raise ApiConfigurationRejected("API CA requires bytes within the 1048576-byte limit.")
    return ApiSettings(identity, _https_from_material(table, "API", ca_bytes), table["topology"]), provider_ref
