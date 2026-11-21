"""Encode one terminal result with the material its independent publisher needs."""

from provider_core.container_guard import require_container

require_container()

import base64
import binascii
import hashlib
import json
import reprlib
from dataclasses import dataclass

from provider_core.api_config import ApiPublicationMaterial, api_settings_from_material
from provider_core.attempt_record import TerminalOperation
from provider_core.delivery_store import DeliveryOutcome
from provider_core.response_json import ResponseRejected, read_document
from provider_core.type_requirements import require_type

# A 2 MiB terminal body and bounded 1 MiB CA plus credential fit with base64 overhead.
# repo-check: allow-module-constant
MAX_HANDOFF_BYTES = 5 * 1024 * 1024


class DeliveryHandoffRejected(ValueError):
    """A handoff cannot be admitted without its exact result and publication inputs."""


def _encoded(raw_bytes):
    return base64.b64encode(raw_bytes).decode("ascii")


def _decoded(encoded_text):
    require_type(encoded_text, str, error_factory=DeliveryHandoffRejected)

    try:
        decoded_bytes = base64.b64decode(encoded_text, validate=True)
    except (ValueError, binascii.Error) as cause:
        raise DeliveryHandoffRejected("Cannot decode the handoff's base64 field") from cause

    canonical_base64 = _encoded(decoded_bytes)

    if canonical_base64 != encoded_text:
        # Fields can encode a signing credential or a multi-megabyte result
        encoded_text_sha256 = hashlib.sha256(encoded_text.encode("ascii")).hexdigest()
        canonical_base64_sha256 = hashlib.sha256(canonical_base64.encode("ascii")).hexdigest()

        raise DeliveryHandoffRejected(f"{encoded_text_sha256=} should match {canonical_base64_sha256=}")

    return decoded_bytes


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
        require_type(
            (actual_outcome := self.outcome),
            DeliveryOutcome,
            error_factory=DeliveryHandoffRejected,
        )

        require_type(
            (actual_publication := self.publication),
            ApiPublicationMaterial,
            error_factory=DeliveryHandoffRejected,
        )

        outcome_api_origin, outcome_provider_ref = self.outcome.api_origin, self.outcome.provider_ref
        publication_api_origin, publication_provider_ref = self.publication.origin, self.publication.provider_ref

        if not (outcome_api_origin == publication_api_origin):
            raise DeliveryHandoffRejected(f"{outcome_api_origin=} should match {publication_api_origin=}")

        if not (outcome_provider_ref == publication_provider_ref):
            raise DeliveryHandoffRejected(f"{outcome_provider_ref=} should match {publication_provider_ref=}")

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

        handoff_byte_length = len(encoded)

        if handoff_byte_length > MAX_HANDOFF_BYTES:
            raise DeliveryHandoffRejected(f"{handoff_byte_length=} should be at most {MAX_HANDOFF_BYTES=}")

        return encoded


def read_delivery_handoff(raw):
    """Admit complete material without exposing request or credential contents."""

    try:
        handoff_fields = read_document(raw, MAX_HANDOFF_BYTES)

        require_type(handoff_fields, dict, error_factory=DeliveryHandoffRejected)

        expected_handoff_fields = {"api", "credential", "ca", "outcome"}
        observed_handoff_fields = set(handoff_fields)

        if observed_handoff_fields != expected_handoff_fields:
            missing_handoff_fields = expected_handoff_fields - observed_handoff_fields
            unexpected_handoff_fields = observed_handoff_fields - expected_handoff_fields

            raise DeliveryHandoffRejected(f"{missing_handoff_fields=} and {reprlib.repr(unexpected_handoff_fields)=} should be empty")

        (api, outcome) = (handoff_fields["api"], handoff_fields["outcome"])

        require_type(api, dict, error_factory=DeliveryHandoffRejected)

        require_type(outcome, dict, error_factory=DeliveryHandoffRejected)

        expected_outcome_fields = {
            "api_origin",
            "provider_ref",
            "job_ref",
            "execution_attempt_ref",
            "operation",
            "body"
        }
        observed_outcome_fields = set(outcome)

        if observed_outcome_fields != expected_outcome_fields:
            missing_outcome_fields = expected_outcome_fields - observed_outcome_fields
            unexpected_outcome_fields = observed_outcome_fields - expected_outcome_fields

            raise DeliveryHandoffRejected(f"{missing_outcome_fields=} and {reprlib.repr(unexpected_outcome_fields)=} should be empty")

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
    except (ResponseRejected, KeyError, TypeError, ValueError, OverflowError) as cause:
        raise DeliveryHandoffRejected("Cannot admit the publication handoff") from cause
