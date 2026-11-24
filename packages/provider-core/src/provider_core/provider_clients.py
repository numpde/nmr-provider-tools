"""Construct independent API and delivery clients from one captured deployment.

Settings and publication material outlive their source files. Each service role
gets its own exchange owner; the caller retires every client it requests.
"""

from provider_core.container_guard import require_container

require_container()

from dataclasses import replace

from provider_core.api import ProviderAPI
from provider_core.api_config import api_settings_with_material
from provider_core.shared_delivery_client import SharedDeliveryClient
from provider_core.startup_file import read_startup_file


class ProviderClients:
    """Share admitted configuration, never an in-flight exchange."""

    def __init__(self, settings, publication, endpoint, *, delivery_exchange_seconds, delivery_retirement_seconds):
        self.settings = settings
        self.publication = publication
        self.endpoint = endpoint
        self.delivery_exchange_seconds = delivery_exchange_seconds
        self.delivery_retirement_seconds = delivery_retirement_seconds

    @classmethod
    def from_config(cls, api, *, grants, read_file=read_startup_file):
        """Capture credentials and trust before opening any provider transport."""
        (settings, provider_ref, publication) = api_settings_with_material(api, read_file)
        settings = replace(settings, http=replace(settings.http, proxy_socket=str(grants.proxy_socket)))

        return cls(
            settings, publication, grants.endpoint(settings.identity.origin, provider_ref),
            delivery_exchange_seconds=settings.http.exchange_seconds,
            delivery_retirement_seconds=settings.http.retirement_seconds,
        )

    @property
    def provider_ref(self):
        return self.publication.provider_ref

    def new_api(self):
        """Create a separate exchange owner for discovery, Hello or an Attempt."""
        return ProviderAPI(
            self.settings.identity,
            self.settings.http.new_transport(),
            expected_topology=self.settings.expected_topology,
        )

    def new_delivery(self):
        """Give an Attempt its own outbox exchange with the captured publication inputs."""
        return SharedDeliveryClient(
            self.endpoint,
            self.publication,
            exchange_seconds=self.delivery_exchange_seconds,
            retirement_seconds=self.delivery_retirement_seconds,
        )
