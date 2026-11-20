"""Load frozen delivery bindings derived from providers' admitted API settings.

Operators do not maintain another copy of signing identities or destinations.
The deployment emits this closed service document; core reuses the existing
credential, TLS and route owners before acquiring SQLite, listeners or API I/O.
"""

from provider_core.container_guard import require_container

require_container()

import reprlib
import tomllib
from dataclasses import replace

from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings
from provider_core.delivery_client import DeliveryEndpoint
from provider_core.delivery_service import MAX_BINDINGS, DeliveryBinding, DeliveryService
from provider_core.delivery_store import QueueLimits
from provider_core.startup_file import StartupFileRejected, read_startup_file
from provider_core.tools import require_absolute_path
from provider_core.type_requirements import require_type


class DeliveryConfigurationRejected(ValueError):
    """Safe configuration context; original admission evidence stays protected."""

    # A closed-field refusal has no underlying exception; parser and existing owner refusals retain their original cause.
    def __init__(self, reason, *, cause=None):
        super().__init__(f"Cannot configure local delivery: {reason=}")
        self.cause = cause


def _fields(table, expected_fields, section_name):
    require_type(
        table,
        dict,
        error_factory=lambda diagnostic: DeliveryConfigurationRejected(f"{section_name=}; " + diagnostic),
    )

    if not ((actual_fields := set(table)) == expected_fields):
        raise DeliveryConfigurationRejected(f"{section_name=}; {reprlib.repr(actual_fields)=} should match {expected_fields=}")


def _binding(table):
    _fields(table, {"api", "socket_path", "socket_group_id", "proxy_socket"}, "binding")
    (settings, provider_ref) = api_settings(table["api"], read_startup_file)

    endpoint = DeliveryEndpoint(
        settings.identity.origin,
        provider_ref,
        require_absolute_path(table["socket_path"]),
        socket_group_id=table["socket_group_id"]
    )

    http = replace(settings.http, proxy_socket=str(require_absolute_path(table["proxy_socket"])))
    client = ProviderAPI(settings.identity, http.new_transport(), expected_topology=settings.expected_topology)

    return DeliveryBinding(endpoint, client)


def load_delivery_service(path, *, on_notice, diagnostic_status):
    """Consume immutable received bytes; construct owners without starting them."""

    try:
        raw = read_startup_file(path, maximum_bytes=65536)
    except StartupFileRejected as cause:
        raise DeliveryConfigurationRejected(str(cause), cause=cause) from None

    try:
        service_configuration = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as cause:
        raise DeliveryConfigurationRejected("require UTF-8 TOML within 65536 bytes.", cause=cause) from None

    _fields(
        service_configuration,
        {
            "schema_id",
            "state_directory",
            "request_seconds",
            "retry_initial_seconds",
            "retry_max_seconds",
            "limits",
            "bindings",
        },
        "configuration"
    )

    expected_schema_id = "provider-core.delivery.v1"

    if not (service_configuration["schema_id"] == expected_schema_id):
        raise DeliveryConfigurationRejected(f"{reprlib.repr(service_configuration['schema_id'])=} should match {expected_schema_id=}")

    _fields(service_configuration["limits"], {"provider_items", "provider_bytes", "total_items", "total_bytes"}, "limits")
    bindings = service_configuration["bindings"]

    require_type(bindings, list, error_factory=DeliveryConfigurationRejected)

    min_bindings = 1

    if not (min_bindings <= len(bindings) <= MAX_BINDINGS):
        raise DeliveryConfigurationRejected(f"Expected {min_bindings=} ≤ {len(bindings)=} ≤ {MAX_BINDINGS=}")

    try:
        # Only existing code-owned admission errors cross this translation.
        # TOML parsing is outside it so parser excerpts never become messages.
        return DeliveryService(
            require_absolute_path(service_configuration["state_directory"]),
            limits=QueueLimits(**service_configuration["limits"]),
            # repo-check: allow-tuple-generator -- Freeze the completely admitted binding set before constructing its service.
            bindings=tuple(_binding(table) for table in bindings),
            request_seconds=service_configuration["request_seconds"],
            retry_initial_seconds=service_configuration["retry_initial_seconds"],
            retry_max_seconds=service_configuration["retry_max_seconds"],
            on_notice=on_notice,
            diagnostic_status=diagnostic_status,
        )
    except DeliveryConfigurationRejected:
        raise
    except OverflowError as cause:
        raise DeliveryConfigurationRejected("time limits must fit the runtime's finite seconds representation.", cause=cause) from None
    except (TypeError, ValueError) as cause:
        raise DeliveryConfigurationRejected(str(cause), cause=cause) from None
