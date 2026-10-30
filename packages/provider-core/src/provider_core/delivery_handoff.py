"""Encode one terminal result with the material its independent publisher needs."""

from provider_core.container_guard import require_container

require_container()

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass

from provider_core.api_config import ApiPublicationMaterial, api_settings_from_material
from provider_core.attempt_record import TerminalOperation
from provider_core.delivery_store import DeliveryOutcome
from provider_core.response_json import ResponseRejected, read_document

# A 2 MiB terminal body and bounded 1 MiB CA plus credential fit with base64 overhead.
# repo-check: allow-module-constant
MAX_HANDOFF_BYTES = 5 * 1024 * 1024


class DeliveryHandoffRejected(ValueError):
    """A handoff cannot be admitted without its exact result and publication inputs."""


def _encoded(value):
    return base64.b64encode(value).decode("ascii")


def _decoded(value):
    if type(value) is not str:
        raise DeliveryHandoffRejected("Delivery handoff requires encoded immutable bytes.")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise DeliveryHandoffRejected("Delivery handoff contains invalid base64 bytes.") from None
    if _encoded(raw) != value:
        raise DeliveryHandoffRejected("Delivery handoff requires canonical base64 bytes.")
    return raw


@dataclass(frozen=True, slots=True, repr=False)
class DeliveryHandoff:
    outcome: DeliveryOutcome
    publication: ApiPublicationMaterial

    def __post_init__(self):
        if type(self.outcome) is not DeliveryOutcome or type(self.publication) is not ApiPublicationMaterial:
            raise DeliveryHandoffRejected("Delivery handoff requires an outcome and its publication material.")
        if (self.outcome.api_origin, self.outcome.provider_ref) != (self.publication.origin, self.publication.provider_ref):
            raise DeliveryHandoffRejected("Delivery outcome and publication material require the same API and provider identity.")

    def publication_document(self):
        return {"api": self.publication.api_fields(), "credential": _encoded(self.publication.credential_bytes),
                "ca": None if self.publication.ca_bytes is None else _encoded(self.publication.ca_bytes)}

    def acceptance(self):
        publication = self.publication_bytes()
        return self.outcome.acceptance() | {"publication_sha256": "sha256:" + hashlib.sha256(publication).hexdigest()}

    def publication_bytes(self):
        return json.dumps(self.publication_document(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")

    def encode(self):
        item = self.outcome
        document = self.publication_document() | {
            "outcome": {"api_origin": item.api_origin, "provider_ref": item.provider_ref, "job_ref": item.job_ref,
                        "execution_attempt_ref": item.execution_attempt_ref, "operation": item.operation.value,
                        "body": _encoded(item.body)}
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
        if len(encoded) > MAX_HANDOFF_BYTES:
            raise DeliveryHandoffRejected("Delivery handoff exceeds its bounded submission size.")
        return encoded


def read_delivery_handoff(raw):
    """Admit complete material without exposing request or credential contents."""
    try:
        document = read_document(raw, MAX_HANDOFF_BYTES)
        if type(document) is not dict or set(document) != {"api", "credential", "ca", "outcome"}:
            raise DeliveryHandoffRejected("Delivery handoff requires its documented fields.")
        api, outcome = document["api"], document["outcome"]
        if type(api) is not dict or type(outcome) is not dict or set(outcome) != {
            "api_origin", "provider_ref", "job_ref", "execution_attempt_ref", "operation", "body"
        }:
            raise DeliveryHandoffRejected("Delivery handoff requires complete API and outcome fields.")
        credential = _decoded(document["credential"])
        ca = None if document["ca"] is None else _decoded(document["ca"])
        settings, provider_ref = api_settings_from_material(api, credential_bytes=credential, ca_bytes=ca)
        material = ApiPublicationMaterial(settings.identity.origin, api["profile"], settings.expected_topology,
                                          provider_ref, settings.http.exchange_seconds, settings.http.retirement_seconds,
                                          credential, ca)
        item = DeliveryOutcome(outcome["api_origin"], outcome["provider_ref"], outcome["job_ref"],
                               outcome["execution_attempt_ref"], TerminalOperation(outcome["operation"]),
                               _decoded(outcome["body"]))
        return DeliveryHandoff(item, material)
    except DeliveryHandoffRejected:
        raise
    except (ResponseRejected, KeyError, TypeError, ValueError, OverflowError):
        raise DeliveryHandoffRejected("Delivery handoff does not satisfy the supported publication contract.") from None
