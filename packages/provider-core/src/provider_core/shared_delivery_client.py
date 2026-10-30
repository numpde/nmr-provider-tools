"""Submit a result and its original publication inputs to shared delivery."""

from provider_core.container_guard import require_container

require_container()

from provider_core.delivery_client import DeliveryClient, DeliveryRejected
from provider_core.delivery_handoff import DeliveryHandoff


class SharedDeliveryClient:
    def __init__(self, endpoint, publication, *, exchange_seconds, retirement_seconds):
        self.endpoint, self.publication = endpoint, publication
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
        reply = await self._client._request("POST", "/submit", body=handoff.encode())
        if reply != handoff.acceptance():
            raise DeliveryRejected("The durable acknowledgement does not match the submitted result and publication material.")
        return reply
