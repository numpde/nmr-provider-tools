"""Submit a result and its original publication inputs to shared delivery."""

from provider_core.container_guard import require_container

require_container()

from provider_core.delivery_client import DeliveryClient, DeliveryRejected
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.api_config import ApiPublicationMaterial
from provider_core.type_requirements import require_type


class SharedDeliveryClient:
    def __init__(self, endpoint, publication, *, exchange_seconds, retirement_seconds):
        require_type(publication, ApiPublicationMaterial)

        if (endpoint.api_origin, endpoint.provider_ref) != (publication.origin, publication.provider_ref):
            raise ValueError("Delivery endpoint and publication material require the same API and provider identity.")

        (self.endpoint, self.publication) = (endpoint, publication)
        self._client = DeliveryClient(endpoint, exchange_seconds=exchange_seconds, retirement_seconds=retirement_seconds)

    @property
    def retirement_confirmed(self):
        return self._client.retirement_confirmed

    @property
    def last_failure(self):
        return self._client.last_failure

    async def retire(self):
        await self._client.retire()

    def can_retry(self, failure):
        return self._client.can_retry(failure)

    async def submit(self, item):
        if item.key[:2] != (self.endpoint.api_origin, self.endpoint.provider_ref):
            raise ValueError("The outcome belongs to another configured provider.")

        handoff = DeliveryHandoff(item, self.publication)
        acceptance_receipt = await self._client._exchange_ipc_request("POST", "/submit", body=handoff.encode())

        if acceptance_receipt != handoff.acceptance():
            raise DeliveryRejected("The durable acknowledgement does not match the submitted result and publication material.")

        return acceptance_receipt
