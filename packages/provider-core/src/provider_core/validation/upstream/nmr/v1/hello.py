"""Admit a complete announcement and its identity-bound receipt.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/routes.py::PROVIDER_HELLO_ROUTE,
nmr_api/provider/schemas/hello_{request,response}.v1.schema.json and
nmr_api/operations/provider_directory.py::ProviderHelloCommand.
Adapts SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/hello.py:72-151, reusing core's existing admission.
Composition owns content, cadence and whether a current snapshot should be sent.
"""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass, field
from datetime import datetime

from provider_core.validation.upstream.nmr.v1.attempt_request import require_analysis_kind
from provider_core.response_json import ResponseRejected, read_document
from provider_core.validation.upstream.nmr.v1.timestamps import read_timestamp
from provider_core.type_requirements import require_type

# The pinned Hello response owns this byte ceiling.
# repo-check: allow-module-constant
HELLO_RESPONSE_BYTES = 65536


@dataclass(frozen=True, slots=True)
class AnalysisOffering:
    analysis_kind_ref: str
    description: str = field(repr=False)

    def __post_init__(self):
        require_analysis_kind(self.analysis_kind_ref)
        _require_text(self.description, "offering description", 1024)


@dataclass(frozen=True, slots=True)
class HelloAccepted:
    """Server acceptance then, not readiness or a promise the snapshot is current."""

    provider_ref: str
    accepted_at: datetime


def _require_text(value, name, maximum):
    if not (type(value) is (expected_type := str)):
        raise TypeError(f"{name=}; {type(value)=} ≠ {expected_type=}")

    if not (1 <= len(value) <= maximum):
        raise ValueError(f"Cannot announce provider: {name} has {len(value)} characters; expected 1 through {maximum} Unicode scalars")

    # These API fields forbid NUL; U+D800–U+DFFF are surrogates, not Unicode scalar values
    if ("\0" in value) or any((0xD800 <= ord(character) <= 0xDFFF) for character in value):
        raise ValueError(f"Cannot announce provider: {name} must not contain NUL or surrogate characters")


def require_hello_inputs(*, display_name, description, analysis_offerings):
    """Admit the complete snapshot without selecting its content or cadence."""

    _require_text(display_name, "display name", 128)
    _require_text(description, "description", 1024)

    require_type(analysis_offerings, tuple)

    if not (len(analysis_offerings) <= 64):
        raise ValueError("Cannot announce provider: analysis offerings must be a tuple of at most 64 entries")

    kinds = set()

    for offering in analysis_offerings:
        require_type(offering, AnalysisOffering)

        if offering.analysis_kind_ref in kinds:
            raise ValueError("Cannot announce provider: analysis kinds must not repeat")

        kinds.add(offering.analysis_kind_ref)


def require_hello_body_size(body):
    """Apply the route ceiling to the actual UTF-8 encoding, not character counts."""

    if not (len(body) <= (512 * 1024)):
        raise ValueError("Cannot announce provider: encoded Hello exceeds the 524288-byte route limit")


def read_hello(raw, *, provider_ref):
    """Bind a closed receipt to composition's expected provider, never to key spelling."""
    document = read_document(raw, HELLO_RESPONSE_BYTES)

    if not (type(document) is (expected_type := dict)):
        raise ResponseRejected(f"{type(document)=} ≠ {expected_type=}")

    if set(document) != {"schema_id", "provider_ref", "accepted_at"}:
        raise ResponseRejected("Response fields do not match the supported Hello receipt schema")

    if document["schema_id"] != "nmr.provider.hello_response.v1":
        raise ResponseRejected("Response schema does not match the Hello receipt contract")

    if document["provider_ref"] != provider_ref:
        raise ResponseRejected("Hello receipt provider differs from the configured provider")

    return HelloAccepted(provider_ref, read_timestamp(document["accepted_at"]))
