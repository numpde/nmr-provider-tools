"""Runtime roles retain captured publication inputs and retire independently."""

import unittest
from unittest.mock import Mock

from provider_core.provider_clients import ProviderClients
from provider_core.shared_grants import SharedGrants
from tests.fakes.credential import document, encoded


class ProviderClientsTests(unittest.IsolatedAsyncioTestCase):
    async def test_clients_survive_removed_configuration_without_sharing_exchange_owners(self):
        credential_bytes = encoded(document())
        reader = Mock(return_value=credential_bytes)

        configuration = {
            "origin": "https://api.example",
            "profile": "dev-local",
            "topology": "dev-local",
            "provider_ref": "provider:example",
            "exchange_seconds": 1,
            "retirement_seconds": 1,
            "credential_file": "/selected/credential.json",
        }

        grants = SharedGrants("nmr-api", 1000)
        clients = ProviderClients.from_config(configuration, grants=grants, read_file=reader)

        reader.side_effect = FileNotFoundError("Configuration was unmounted after startup")
        discovery = clients.new_api()
        announcement = clients.new_api()
        first_delivery = clients.new_delivery()
        second_delivery = clients.new_delivery()

        independently_timed = ProviderClients(
            clients.settings, clients.publication, clients.endpoint,
            delivery_exchange_seconds=7, delivery_retirement_seconds=3,
        ).new_delivery()

        try:
            self.assertEqual(clients.provider_ref, "provider:example")
            self.assertEqual(clients.settings.http.proxy_socket, str(grants.proxy_socket))
            self.assertIsNot(discovery._transport, announcement._transport)
            self.assertEqual(discovery._transport._proxy_socket, str(grants.proxy_socket))
            self.assertIsNot(first_delivery, second_delivery)
            self.assertIs(first_delivery.publication, second_delivery.publication)
            self.assertEqual(first_delivery.publication.credential_bytes, credential_bytes)
            self.assertEqual(first_delivery.endpoint.socket_path, grants.outbox_socket)
            self.assertEqual(independently_timed._client._transport.exchange_seconds, 7)
            self.assertEqual(independently_timed._client._transport.retirement_seconds, 3)
            self.assertEqual(discovery.exchange_seconds, 1)
            reader.assert_called_once_with("/selected/credential.json", maximum_bytes=16384, secret=True)
            await discovery.retire()
            self.assertTrue(discovery.retirement_confirmed)
            self.assertIsNot(discovery, announcement)
        finally:
            await discovery.retire()
            await announcement.retire()
            await first_delivery.retire()
            await second_delivery.retire()
            await independently_timed.retire()
