"""Bind exact instructions to the authenticated Job/feed observations of the caller.

Wire authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/job_input_read_response.v1.schema.json. Response ceiling
and consistency precedent: magnet-deploy c5813ee35cfbb1c98a5cc5d20a94787271f26486,
magnet_provider/job_input.py. This module verifies consistency, not network trust.
"""

from provider_core.container_guard import require_container

require_container()

import base64
import hashlib
import re
from dataclasses import dataclass, field

from provider_core.response_json import ResponseRejected, read_document

# The admitted input profile has this fixed protocol identity.
# repo-check: allow-module-constant
INPUT_SCHEMA = "nmr.job.specification.text.v1"

# The pinned input response has this fixed protocol identity.
# repo-check: allow-module-constant
RESPONSE_SCHEMA = "nmr.provider.job_input.read.response.v1"

# The protocol bounds exact Job input bytes before scientific admission.
# repo-check: allow-module-constant
MAX_INPUT_BYTES = 65536

# The pinned input response has this byte ceiling.
# repo-check: allow-module-constant
MAX_RESPONSE_BYTES = 131072


class JobInputRejected(ValueError):
    """An observed input fails the wire/identity contract, before scientific work."""


@dataclass(frozen=True, slots=True)
class FeedInputFacts:
    """Admitted identity facts; they do not themselves grant access to a Job."""

    job_ref: str
    input_schema_id: str
    input_fingerprint: str
    input_byte_length: int

    def __post_init__(self):
        if not (type(self.job_ref) is (expected_type := str)):
            raise JobInputRejected(f"{type(self.job_ref)=} ≠ {expected_type=}")

        if not re.fullmatch((expected_pattern := r"job:[A-Za-z0-9_.-]{1,124}"), self.job_ref):
            raise JobInputRejected(f"{self.job_ref=} should match {expected_pattern=}")

        if not (self.input_schema_id == INPUT_SCHEMA):
            raise JobInputRejected(f"{self.input_schema_id=} should match {INPUT_SCHEMA=}")

        if not (type(self.input_fingerprint) is (expected_type := str)):
            raise JobInputRejected(f"{type(self.input_fingerprint)=} ≠ {expected_type=}")

        if not re.fullmatch((expected_pattern := r"sha256:[0-9a-f]{64}"), self.input_fingerprint):
            raise JobInputRejected(f"{self.input_fingerprint=} should match {expected_pattern=}")

        if not (type(self.input_byte_length) is (expected_type := int)):
            raise JobInputRejected(f"{type(self.input_byte_length)=} ≠ {expected_type=}")

        if not (1 <= self.input_byte_length <= MAX_INPUT_BYTES):
            raise JobInputRejected(f"{self.input_byte_length=} should be between 1 and {MAX_INPUT_BYTES=}")


@dataclass(frozen=True, slots=True)
class VerifiedJobInput:
    """One immutable payload, validated even when constructed by a direct consumer."""

    identity: FeedInputFacts
    specification_bytes: bytes = field(repr=False)

    def __post_init__(self):
        if not (type(self.identity) is (expected_type := FeedInputFacts)):
            raise JobInputRejected(f"{type(self.identity)=} ≠ {expected_type=}")

        if not (type(self.specification_bytes) is (expected_type := bytes)):
            raise JobInputRejected(f"{type(self.specification_bytes)=} ≠ {expected_type=}")

        specification_byte_length = len(self.specification_bytes)

        if not (specification_byte_length == self.identity.input_byte_length):
            raise JobInputRejected(f"{specification_byte_length=} should match {self.identity.input_byte_length=}")

        specification_fingerprint = "sha256:" + hashlib.sha256(self.specification_bytes).hexdigest()

        if not (specification_fingerprint == self.identity.input_fingerprint):
            raise JobInputRejected(f"{specification_fingerprint=} should match {self.identity.input_fingerprint=}")

        try:
            self.specification_bytes.decode("utf-8")
        except UnicodeDecodeError as cause:
            # A full bytes repr can quadruple the input size; identify the Job here
            # and retain the decoder's offending byte range and reason as the cause
            raise JobInputRejected(f"Cannot decode input for {self.job_ref=}") from cause

    @property
    def job_ref(self):
        return self.identity.job_ref

    @property
    def specification_text(self):
        return self.specification_bytes.decode("utf-8")


def verify_job_input(response_bytes, *, feed):
    """Reject substituted observations before returning exact original text bytes.

    Envelope whitespace/key order is immaterial; duplicate/unknown fields and
    noncanonical base64 are rejected. The source payload is never reserialized.
    """

    if not (type(feed) is (expected_type := FeedInputFacts)):
        raise JobInputRejected(f"{type(feed)=} ≠ {expected_type=}")

    try:
        input_response = read_document(response_bytes, MAX_RESPONSE_BYTES)
    except ResponseRejected as cause:
        raise JobInputRejected(str(cause)) from cause

    if not (type(input_response) is (expected_type := dict)):
        raise JobInputRejected(f"{type(input_response)=} ≠ {expected_type=}")

    response_fields = set(input_response)
    expected_fields = {"schema_id", "job_ref", "input_schema_id", "input_fingerprint", "input_byte_length", "canonical_input_base64"}

    if not (response_fields == expected_fields):
        raise JobInputRejected(f"{response_fields=} should match {expected_fields=}")

    if not ((response_schema_id := input_response["schema_id"]) == RESPONSE_SCHEMA):
        raise JobInputRejected(f"{response_schema_id=} should match {RESPONSE_SCHEMA=}")

    observed_input = FeedInputFacts(
        *(input_response[name] for name in ("job_ref", "input_schema_id", "input_fingerprint", "input_byte_length"))
    )

    for field_name in ("job_ref", "input_schema_id", "input_fingerprint", "input_byte_length"):
        observed_input_fact = getattr(observed_input, field_name)
        feed_input_fact = getattr(feed, field_name)

        if not (observed_input_fact == feed_input_fact):
            raise JobInputRejected(f"{field_name=}; {observed_input_fact=} should match {feed_input_fact=}")

    observed_input_base64 = input_response["canonical_input_base64"]

    if not (type(observed_input_base64) is (expected_type := str)):
        raise JobInputRejected(f"{type(observed_input_base64)=} ≠ {expected_type=}")

    base64_character_count = len(observed_input_base64)

    if not (1 <= base64_character_count <= 87384):
        raise JobInputRejected(f"{base64_character_count=} should be between 1 and 87384")

    try:
        specification_bytes = base64.b64decode(observed_input_base64, validate=True)
    except ValueError as cause:
        raise JobInputRejected(f"Cannot decode input for {feed.job_ref=}") from cause

    expected_input_base64 = base64.b64encode(specification_bytes).decode("ascii")

    if not (observed_input_base64 == expected_input_base64):
        raise JobInputRejected(f"{observed_input_base64[-4:]=} should match {expected_input_base64[-4:]=}")

    return VerifiedJobInput(feed, specification_bytes)
