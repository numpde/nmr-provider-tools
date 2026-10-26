"""Bind exact instructions to the authenticated Job/feed observations of the caller.

Wire authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/job_input_read_response.v1.schema.json. Response ceiling
and consistency precedent: magnet-deploy c5813ee35cfbb1c98a5cc5d20a94787271f26486,
magnet_provider/job_input.py. This module verifies consistency, not network trust.
"""

from provider_core.container_guard import require_container

require_container()

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field

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
        if type(self.job_ref) is not str or re.fullmatch(r"job:[A-Za-z0-9_.-]{1,124}", self.job_ref) is None:
            raise JobInputRejected("Cannot verify Job input: job_ref is not a valid Job reference.")
        if self.input_schema_id != INPUT_SCHEMA:
            raise JobInputRejected("Cannot verify Job input: input_schema_id is not the supported text schema.")
        if type(self.input_fingerprint) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", self.input_fingerprint) is None:
            raise JobInputRejected("Cannot verify Job input: input_fingerprint must be a lowercase SHA-256 fingerprint.")
        if type(self.input_byte_length) is not int or not 1 <= self.input_byte_length <= MAX_INPUT_BYTES:
            raise JobInputRejected("Cannot verify Job input: input_byte_length must be an integer from 1 through 65536.")


@dataclass(frozen=True, slots=True)
class VerifiedJobInput:
    """One immutable payload, validated even when constructed by a direct consumer."""

    identity: FeedInputFacts
    specification_bytes: bytes = field(repr=False)

    def __post_init__(self):
        if type(self.identity) is not FeedInputFacts or type(self.specification_bytes) is not bytes:
            raise JobInputRejected("Cannot verify Job input: admitted identity and immutable bytes are required.")
        if len(self.specification_bytes) != self.identity.input_byte_length:
            raise JobInputRejected("Cannot verify Job input: decoded byte length differs from the feed identity.")
        if "sha256:" + hashlib.sha256(self.specification_bytes).hexdigest() != self.identity.input_fingerprint:
            raise JobInputRejected("Cannot verify Job input: decoded SHA-256 differs from the feed identity.")
        try:
            self.specification_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise JobInputRejected(f"Cannot verify Job input: invalid UTF-8 at byte {error.start}.") from None

    @property
    def job_ref(self):
        return self.identity.job_ref

    @property
    def specification_text(self):
        return self.specification_bytes.decode("utf-8")


def _unique_fields(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise JobInputRejected("Cannot verify Job input: response contains a duplicate JSON field.")
        document[key] = value
    return document


def _reject_constant(_value):
    raise JobInputRejected("Cannot verify Job input: response contains a nonfinite JSON number.")


def _read_response(raw):
    if type(raw) is not bytes:
        raise JobInputRejected("Cannot verify Job input: response must be immutable bytes.")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JobInputRejected("Cannot verify Job input: response exceeds the 131072-byte limit.")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
    except JobInputRejected:
        raise
    except UnicodeDecodeError as error:
        raise JobInputRejected(f"Cannot verify Job input: response has invalid UTF-8 at byte {error.start}.") from None
    except json.JSONDecodeError as error:
        raise JobInputRejected(
            f"Cannot verify Job input: invalid response JSON at line {error.lineno}, column {error.colno}."
        ) from None
    except (RecursionError, ValueError):
        raise JobInputRejected("Cannot verify Job input: response JSON exceeds parser nesting or number limits.") from None


def verify_job_input(response_bytes, *, feed):
    """Reject substituted observations before returning exact original text bytes.

    Envelope whitespace/key order is immaterial; duplicate/unknown fields and
    noncanonical base64 are rejected. The source payload is never reserialized.
    """
    if type(feed) is not FeedInputFacts:
        raise JobInputRejected("Cannot verify Job input: admitted feed identity is required.")
    document = _read_response(response_bytes)
    fields = {"schema_id", "job_ref", "input_schema_id", "input_fingerprint", "input_byte_length", "canonical_input_base64"}
    if type(document) is not dict or set(document) != fields:
        raise JobInputRejected("Cannot verify Job input: response must contain exactly the six input-read fields.")
    if document["schema_id"] != RESPONSE_SCHEMA:
        raise JobInputRejected("Cannot verify Job input: response schema_id is unsupported.")
    observed = FeedInputFacts(
        *(document[name] for name in ("job_ref", "input_schema_id", "input_fingerprint", "input_byte_length"))
    )
    for name in ("job_ref", "input_schema_id", "input_fingerprint", "input_byte_length"):
        if getattr(observed, name) != getattr(feed, name):
            raise JobInputRejected(f"Cannot verify Job input: response {name} differs from the feed identity.")
    encoded = document["canonical_input_base64"]
    if type(encoded) is not str or not 1 <= len(encoded) <= 87384:
        raise JobInputRejected("Cannot verify Job input: canonical_input_base64 must be bounded nonempty text.")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise JobInputRejected("Cannot verify Job input: invalid base64 encoding.") from None
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise JobInputRejected("Cannot verify Job input: base64 padding or pad bits are not canonical.")
    return VerifiedJobInput(feed, payload)
