"""Load frozen delivery bindings derived from providers' admitted API settings.

Operators do not maintain another copy of signing identities or destinations.
The deployment emits this closed service document; core reuses the existing
credential, TLS and route owners before acquiring SQLite, listeners or API I/O.
"""

from provider_core.container_guard import require_container

require_container()

import tomllib
from dataclasses import replace
from pathlib import Path

from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_service import MAX_BINDINGS, DeliveryBinding, DeliveryService
from provider_core.delivery_store import QueueLimits
from provider_core.startup_file import StartupFileRejected, read_startup_file


class DeliveryConfigurationRejected(ValueError):
    """Safe configuration context; original admission evidence stays protected."""

    # A closed-field refusal has no underlying exception; parser and existing owner refusals retain their original cause.
    def __init__(self, message, *, cause=None):
        super().__init__(f"Cannot configure local delivery: {message}")
        self.cause = cause


def _fields(table, fields, label):
    if type(table) is not dict or set(table) != fields:
        raise DeliveryConfigurationRejected(f"{label} must contain exactly its documented fields.")


def _path(value, label):
    if type(value) is not str or not value or "\x00" in value or not Path(value).is_absolute():
        raise DeliveryConfigurationRejected(f"{label} requires an explicit absolute container path.")
    return Path(value)


def _binding(table):
    _fields(table, {"api", "socket_path", "socket_group_id", "proxy_socket"}, "binding")
    settings, provider_ref = api_settings(table["api"], read_startup_file)
    endpoint = DeliveryEndpoint(settings.identity.origin, provider_ref, _path(table["socket_path"], "delivery socket"),
                                socket_group_id=table["socket_group_id"])
    http = replace(settings.http, proxy_socket=str(_path(table["proxy_socket"], "API proxy socket")))
    client = ProviderAPI(settings.identity, http.new_transport(), expected_topology=settings.expected_topology)
    return DeliveryBinding(endpoint, client)


def load_delivery_service(path, *, on_notice, diagnostic_status):
    """Consume immutable received bytes; construct owners without starting them."""
    try:
        raw = read_startup_file(path, maximum_bytes=65536, label="delivery configuration")
    except StartupFileRejected as cause:
        raise DeliveryConfigurationRejected(str(cause), cause=cause) from None
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as cause:
        raise DeliveryConfigurationRejected("require UTF-8 TOML within 65536 bytes.", cause=cause) from None
    _fields(document, {
        "schema_id", "state_directory", "request_seconds", "retry_initial_seconds", "retry_max_seconds", "limits", "bindings",
    }, "configuration")
    if document["schema_id"] != "provider-core.delivery.v1":
        raise DeliveryConfigurationRejected("require schema provider-core.delivery.v1.")
    _fields(document["limits"], {"provider_items", "provider_bytes", "total_items", "total_bytes"}, "limits")
    bindings = document["bindings"]
    if type(bindings) is not list or not 1 <= len(bindings) <= MAX_BINDINGS:
        raise DeliveryConfigurationRejected("require between one and sixteen explicit bindings.")
    try:
        # Only existing code-owned admission errors cross this translation.
        # TOML parsing is outside it so parser excerpts never become messages.
        return DeliveryService(
            _path(document["state_directory"], "delivery state"), limits=QueueLimits(**document["limits"]),
            # repo-check: allow-tuple-generator -- Freeze the completely admitted binding set before constructing its service.
            bindings=tuple(_binding(table) for table in bindings), request_seconds=document["request_seconds"],
            retry_initial_seconds=document["retry_initial_seconds"], retry_max_seconds=document["retry_max_seconds"],
            on_notice=on_notice, diagnostic_status=diagnostic_status,
        )
    except DeliveryConfigurationRejected:
        raise
    except OverflowError as cause:
        raise DeliveryConfigurationRejected("time limits must fit the runtime's finite seconds representation.", cause=cause) from None
    except ValueError as cause:
        raise DeliveryConfigurationRejected(str(cause), cause=cause) from None
