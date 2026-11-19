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
    if not (type(value) is (expected_type := str)):
        raise DeliveryHandoffRejected(f"{type(value)=} ≠ {expected_type=}")

    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise DeliveryHandoffRejected("Delivery handoff contains invalid base64 bytes.") from None

    if _encoded(raw) != value:
        raise DeliveryHandoffRejected("Delivery handoff requires canonical base64 bytes.")

    return raw


def publication_bytes(material):
    # silent-defaults: allow-contract -- An absent CA explicitly selects the delivery image's system trust roots.
    publication_fields = {
        "api": material.api_fields(),
        "credential": _encoded(material.credential_bytes),
        "ca": None if material.ca_bytes is None else _encoded(material.ca_bytes)
    }

    return (
        json
        .dumps(
            publication_fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        .encode("ascii")
    )


@dataclass(frozen=True, slots=True, repr=False)
class DeliveryHandoff:
    outcome: DeliveryOutcome
    publication: ApiPublicationMaterial

    def __post_init__(self):
        if not (type((actual_outcome := self.outcome)) is (expected_type := DeliveryOutcome)):
            raise DeliveryHandoffRejected(f"{type(actual_outcome)=} ≠ {expected_type=}")

        if not (type((actual_publication := self.publication)) is (expected_type := ApiPublicationMaterial)):
            raise DeliveryHandoffRejected(f"{type(actual_publication)=} ≠ {expected_type=}")

        if (self.outcome.api_origin, self.outcome.provider_ref) != (self.publication.origin, self.publication.provider_ref):
            raise DeliveryHandoffRejected("Delivery outcome and publication material require the same API and provider identity.")

    def publication_document(self):
        # silent-defaults: allow-contract -- An absent CA explicitly selects the delivery image's system trust roots.
        return {
            "api": self.publication.api_fields(),
            "credential": _encoded(self.publication.credential_bytes),
            "ca": None if self.publication.ca_bytes is None else _encoded(self.publication.ca_bytes)
        }

    def acceptance(self):
        publication = self.publication_bytes()

        return self.outcome.acceptance() | {"publication_sha256": "sha256:" + hashlib.sha256(publication).hexdigest()}

    def publication_bytes(self):
        return publication_bytes(self.publication)

    def encode(self):
        delivery_outcome = self.outcome

        handoff_fields = self.publication_document() | {
            "outcome": {
                "api_origin": delivery_outcome.api_origin,
                "provider_ref": delivery_outcome.provider_ref,
                "job_ref": delivery_outcome.job_ref,
                "execution_attempt_ref": delivery_outcome.execution_attempt_ref,
                "operation": delivery_outcome.operation.value,
                "body": _encoded(delivery_outcome.body)
            }
        }

        encoded = json.dumps(handoff_fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")

        if len(encoded) > MAX_HANDOFF_BYTES:
            raise DeliveryHandoffRejected("Delivery handoff exceeds its bounded submission size.")

        return encoded


def read_delivery_handoff(raw):
    """Admit complete material without exposing request or credential contents."""

    try:
        handoff_fields = read_document(raw, MAX_HANDOFF_BYTES)

        if not (type(handoff_fields) is (expected_type := dict)):
            raise DeliveryHandoffRejected(f"{type(handoff_fields)=} ≠ {expected_type=}")

        if set(handoff_fields) != {"api", "credential", "ca", "outcome"}:
            raise DeliveryHandoffRejected("Delivery handoff requires its documented fields.")

        (api, outcome) = (handoff_fields["api"], handoff_fields["outcome"])

        if not (type(api) is (expected_type := dict)):
            raise DeliveryHandoffRejected(f"{type(api)=} ≠ {expected_type=}")

        if not (type(outcome) is (expected_type := dict)):
            raise DeliveryHandoffRejected(f"{type(outcome)=} ≠ {expected_type=}")

        if (set(outcome) != {
            "api_origin",
            "provider_ref",
            "job_ref",
            "execution_attempt_ref",
            "operation",
            "body"
        }):
            raise DeliveryHandoffRejected("Delivery handoff requires complete API and outcome fields.")

        credential = _decoded(handoff_fields["credential"])

        # silent-defaults: allow-contract -- Preserve an explicit absence of CA bytes across the handoff.
        ca = None if handoff_fields["ca"] is None else _decoded(handoff_fields["ca"])

        (settings, provider_ref) = api_settings_from_material(api, credential_bytes=credential, ca_bytes=ca)

        material = ApiPublicationMaterial(
            settings.identity.origin,
            api["profile"],
            settings.expected_topology,
            provider_ref,
            settings.http.exchange_seconds,
            settings.http.retirement_seconds,
            credential,
            ca
        )

        delivery_outcome = DeliveryOutcome(
            outcome["api_origin"],
            outcome["provider_ref"],
            outcome["job_ref"],
            outcome["execution_attempt_ref"],
            TerminalOperation(outcome["operation"]),
            _decoded(outcome["body"])
        )

        return DeliveryHandoff(delivery_outcome, material)
    except DeliveryHandoffRejected:
        raise
    except (ResponseRejected, KeyError, TypeError, ValueError, OverflowError):
        raise DeliveryHandoffRejected("Delivery handoff does not satisfy the supported publication contract.") from None
