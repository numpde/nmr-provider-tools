"""Admit lifecycle observations without inferring work or publication history.

Wire authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/routes.py::EXECUTION_ATTEMPT_READ_ROUTE and
nmr_api/provider/schemas/execution_attempt_read_response.v1.schema.json.
Readback contains no terminal content; exact terminal resend, implemented by
the publication owner, must establish whether retained outcome bytes committed.
"""

from provider_core.container_guard import require_container

require_container()

import re
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

from provider_core.response_json import ResponseRejected, read_document, read_timestamp

# The pinned Attempt response has this wire byte ceiling.
# repo-check: allow-module-constant
ATTEMPT_RESPONSE_BYTES = 65536
# The pinned API owns this closed Job state vocabulary.
# repo-check: allow-module-constant
JOB_STATES = frozenset({"open", "closed", "cancelled"})


@dataclass(frozen=True, slots=True)
class AttemptObservation:
    """Independent point-in-time states, not permission to start or repeat work."""

    execution_attempt_ref: str
    job_ref: str
    state: str
    job_state: str


@dataclass(frozen=True, slots=True)
class AttemptAdmission:
    """Confirmed start identity and snapshot; not proof of local execution entry."""

    execution_attempt_ref: str
    job_ref: str
    analysis_kind_ref: str
    provider_ref: str
    state: str
    started_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class AttemptProgress:
    """Accepted observation and original time, not a heartbeat or execution proof."""

    execution_attempt_ref: str
    phase: str
    condition_code: str | None
    updated_at: datetime


def require_attempt_ref(value):
    if type(value) is not str or re.fullmatch(r"execution_attempt:sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(
            "Execution Attempt reference must be execution_attempt:sha256: followed by 64 lowercase hexadecimal digits"
        )


def read_attempt(raw, *, execution_attempt_ref, job_ref):
    document = read_document(raw, ATTEMPT_RESPONSE_BYTES)
    if type(document) is not dict or set(document) != {"schema_id", "execution_attempt_ref", "job_ref", "state", "job_state"}:
        raise ResponseRejected("Response fields do not match the supported Execution Attempt read schema")
    if document["schema_id"] != "nmr.provider.execution_attempt_read_response.v1":
        raise ResponseRejected("Response schema does not match the Execution Attempt read contract")
    if document["execution_attempt_ref"] != execution_attempt_ref or document["job_ref"] != job_ref:
        raise ResponseRejected("Execution Attempt or Job identity differs from the request")
    if document["state"] not in ("in_progress", "succeeded", "failed", "expired"):
        raise ResponseRejected("Execution Attempt state must be in_progress, succeeded, failed or expired")
    if type(document["job_state"]) is not str or document["job_state"] not in JOB_STATES:
        raise ResponseRejected("Job state must be open, closed or cancelled")
    return AttemptObservation(execution_attempt_ref, job_ref, document["state"], document["job_state"])


def read_start(raw, *, job_ref, analysis_kind_ref, provider_ref):
    """Bind admission to expected catalog identities, including terminal replays.

    Exact wire authority: the pinned API's provider/schemas/
    execution_attempt_start_response.v1.schema.json. Provider identity is checked
    against composition's expectation, never inferred from a credential key name.
    """
    document = read_document(raw, ATTEMPT_RESPONSE_BYTES)
    if type(document) is not dict or set(document) != {
        "schema_id",
        "execution_attempt_ref",
        "job_ref",
        "analysis_kind_ref",
        "provider_ref",
        "state",
        "started_at",
        "replayed",
    }:
        raise ResponseRejected("Response fields do not match the supported Execution Attempt start schema")
    if document["schema_id"] != "nmr.provider.execution_attempt_start_response.v1":
        raise ResponseRejected("Response schema does not match the Execution Attempt start contract")
    if (document["job_ref"], document["analysis_kind_ref"], document["provider_ref"]) != (
        job_ref,
        analysis_kind_ref,
        provider_ref,
    ):
        raise ResponseRejected("Started Attempt Job, analysis kind or provider identity differs from the expected start")
    try:
        require_attempt_ref(document["execution_attempt_ref"])
    except ValueError as cause:
        raise ResponseRejected(str(cause)) from None
    if document["state"] not in ("in_progress", "succeeded", "failed", "expired"):
        raise ResponseRejected("Execution Attempt state must be in_progress, succeeded, failed or expired")
    if type(document["replayed"]) is not bool:
        raise ResponseRejected("Attempt replayed must be a boolean")
    timestamp = read_timestamp(document["started_at"])
    return AttemptAdmission(
        document["execution_attempt_ref"],
        job_ref,
        analysis_kind_ref,
        provider_ref,
        document["state"],
        timestamp,
        document["replayed"],
    )


def read_progress(raw, *, execution_attempt_ref, phase, condition_code):
    """Bind the acknowledged snapshot to the requested observation.

    Pinned API provider/schemas/execution_attempt_progress_response.v1.schema.json.
    Repeating stored facts may succeed after terminalization; updated_at remains
    their original observation time, not proof of continuing work or availability.
    """
    document = read_document(raw, ATTEMPT_RESPONSE_BYTES)
    if type(document) is not dict or set(document) != {
        "schema_id",
        "execution_attempt_ref",
        "phase",
        "condition_code",
        "updated_at",
    }:
        raise ResponseRejected("Response fields do not match the supported Execution Attempt progress schema")
    if document["schema_id"] != "nmr.provider.execution_attempt_progress_response.v1":
        raise ResponseRejected("Response schema does not match the Execution Attempt progress contract")
    if (document["execution_attempt_ref"], document["phase"], document["condition_code"]) != (
        execution_attempt_ref,
        phase,
        condition_code,
    ):
        raise ResponseRejected("Acknowledged Attempt, phase or condition differs from the submitted progress observation")
    return AttemptProgress(execution_attempt_ref, phase, condition_code, read_timestamp(document["updated_at"]))


@dataclass(frozen=True, slots=True)
class AttemptCompletion:
    """Acknowledgement bound to the retained result, not merely terminal state."""

    execution_attempt_ref: str
    analysis_result_ref: str
    result_schema_id: str = field(repr=False)
    result_fingerprint: str
    result_byte_length: int
    committed_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class AttemptFailure:
    """Acknowledgement bound to exact published failure facts."""

    execution_attempt_ref: str
    failure_code: str
    failure_message: str = field(repr=False)
    committed_at: datetime
    replayed: bool


def _outcome_response(raw, operation, execution_attempt_ref, fields):
    # Authority: pinned provider/schemas/execution_attempt_{complete,fail}_response.v1.schema.json.
    document = read_document(raw, ATTEMPT_RESPONSE_BYTES)
    if type(document) is not dict or set(document) != {"schema_id", "execution_attempt_ref", "committed_at", "replayed", *fields}:
        raise ResponseRejected("Response fields do not match the supported Attempt outcome schema")
    if (
        document["schema_id"] != f"nmr.provider.execution_attempt_{operation}_response.v1"
        or document["execution_attempt_ref"] != execution_attempt_ref
    ):
        raise ResponseRejected("Outcome response schema or Attempt identity differs from the publication")
    if type(document["replayed"]) is not bool:
        raise ResponseRejected("Outcome replayed must be a boolean")
    return document


def read_complete(raw, *, execution_attempt_ref, result_schema_id, result):
    document = _outcome_response(
        raw,
        "complete",
        execution_attempt_ref,
        ("analysis_result_ref", "result_schema_id", "result_fingerprint", "result_byte_length"),
    )
    fingerprint = "sha256:" + sha256(result).hexdigest()
    if (document["result_schema_id"], document["result_fingerprint"], document["result_byte_length"]) != (
        result_schema_id,
        fingerprint,
        len(result),
    ) or type(document["result_byte_length"]) is not int:
        raise ResponseRejected("Completion acknowledgement does not match the retained result schema, SHA-256 or byte length")
    result_ref = document["analysis_result_ref"]
    if type(result_ref) is not str or re.fullmatch(r"analysis_result:sha256:[0-9a-f]{64}", result_ref) is None:
        raise ResponseRejected(
            "Analysis result reference must be analysis_result:sha256: followed by 64 lowercase hexadecimal digits"
        )
    return AttemptCompletion(
        execution_attempt_ref,
        result_ref,
        result_schema_id,
        fingerprint,
        len(result),
        read_timestamp(document["committed_at"]),
        document["replayed"],
    )


def read_fail(raw, *, execution_attempt_ref, failure_code, failure_message):
    document = _outcome_response(raw, "fail", execution_attempt_ref, ("failure_code", "failure_message"))
    if (document["failure_code"], document["failure_message"]) != (failure_code, failure_message):
        raise ResponseRejected("Failure acknowledgement does not match the retained failure code and message")
    return AttemptFailure(
        execution_attempt_ref, failure_code, failure_message, read_timestamp(document["committed_at"]), document["replayed"]
    )
