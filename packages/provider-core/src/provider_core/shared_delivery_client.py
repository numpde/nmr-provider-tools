"""Submit a result and its original publication inputs to shared delivery."""

from provider_core.container_guard import require_container

require_container()

import reprlib

from provider_core.delivery_client import DeliveryClient, DeliveryRejected
from provider_core.delivery_handoff import DeliveryHandoff
from provider_core.api_config import ApiPublicationMaterial
from provider_core.type_requirements import require_type


class SharedDeliveryClient:
    def __init__(self, endpoint, publication, *, exchange_seconds, retirement_seconds):
        require_type(publication, ApiPublicationMaterial)

        endpoint_api_origin, endpoint_provider_ref = endpoint.api_origin, endpoint.provider_ref
        publication_api_origin, publication_provider_ref = publication.origin, publication.provider_ref

        if not (endpoint_api_origin == publication_api_origin):
            raise ValueError(f"{endpoint_api_origin=} should match {publication_api_origin=}")

        if not (endpoint_provider_ref == publication_provider_ref):
            raise ValueError(f"{endpoint_provider_ref=} should match {publication_provider_ref=}")

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
        expected_binding = (self.endpoint.api_origin, self.endpoint.provider_ref)

        if item.key[:2] != expected_binding:
            raise ValueError(f"{item.key[:2]=} should match {expected_binding=}")

        handoff = DeliveryHandoff(item, self.publication)
        acceptance_receipt = await self._client._exchange_ipc_request("POST", "/submit", body=handoff.encode())

        expected_acceptance = handoff.acceptance()

        if acceptance_receipt != expected_acceptance:
            # Keep every ordinary receipt field and digest visible without dumping a malformed reply
            receipt_evidence = reprlib.Repr()
            receipt_evidence.maxdict = 8
            receipt_evidence.maxstring = 256

            raise DeliveryRejected(f"{receipt_evidence.repr(acceptance_receipt)=} should match {expected_acceptance=}")

        return acceptance_receipt
