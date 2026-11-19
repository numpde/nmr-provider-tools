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
from provider_core.type_requirements import require_type

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
    require_type(value, str)

    if re.fullmatch(r"execution_attempt:sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("Execution Attempt reference must be execution_attempt:sha256: followed by 64 lowercase hexadecimal digits")


def read_attempt(raw, *, execution_attempt_ref, job_ref):
    attempt_response = read_document(raw, ATTEMPT_RESPONSE_BYTES)

    if not (type(attempt_response) is (expected_type := dict)):
        raise ResponseRejected(f"{type(attempt_response)=} ≠ {expected_type=}")

    response_fields = set(attempt_response)
    expected_fields = {"schema_id", "execution_attempt_ref", "job_ref", "state", "job_state"}

    if not (response_fields == expected_fields):
        raise ResponseRejected(f"{response_fields=} should match {expected_fields=}")

    expected_schema_id = "nmr.provider.execution_attempt_read_response.v1"

    if not ((response_schema_id := attempt_response["schema_id"]) == expected_schema_id):
        raise ResponseRejected(f"{response_schema_id=} should match {expected_schema_id=}")

    if not ((observed_execution_attempt_ref := attempt_response["execution_attempt_ref"]) == execution_attempt_ref):
        raise ResponseRejected(f"{observed_execution_attempt_ref=} should match {execution_attempt_ref=}")

    if not ((observed_job_ref := attempt_response["job_ref"]) == job_ref):
        raise ResponseRejected(f"{observed_job_ref=} should match {job_ref=}")

    if not ((observed_attempt_state := attempt_response["state"]) in ("in_progress", "succeeded", "failed", "expired")):
        raise ResponseRejected(f"{observed_attempt_state=} should be 'in_progress', 'succeeded', 'failed' or 'expired'")

    if not (type((observed_job_state := attempt_response["job_state"])) is (expected_type := str)):
        raise ResponseRejected(f"{type(observed_job_state)=} ≠ {expected_type=}")

    if not (observed_job_state in JOB_STATES):
        raise ResponseRejected(f"{observed_job_state=} should be one of {JOB_STATES=}")

    return AttemptObservation(execution_attempt_ref, job_ref, observed_attempt_state, observed_job_state)


def read_start(raw, *, job_ref, analysis_kind_ref, provider_ref):
    """Bind admission to expected catalog identities, including terminal replays.

    Exact wire authority: the pinned API's provider/schemas/
    execution_attempt_start_response.v1.schema.json. Provider identity is checked
    against composition's expectation, never inferred from a credential key name.
    """

    start_response = read_document(raw, ATTEMPT_RESPONSE_BYTES)

    if not (type(start_response) is (expected_type := dict)):
        raise ResponseRejected(f"{type(start_response)=} ≠ {expected_type=}")

    response_fields = set(start_response)

    expected_fields = {
        "schema_id",
        "execution_attempt_ref",
        "job_ref",
        "analysis_kind_ref",
        "provider_ref",
        "state",
        "started_at",
        "replayed",
    }

    if not (response_fields == expected_fields):
        raise ResponseRejected(f"{response_fields=} should match {expected_fields=}")

    expected_schema_id = "nmr.provider.execution_attempt_start_response.v1"

    if not ((response_schema_id := start_response["schema_id"]) == expected_schema_id):
        raise ResponseRejected(f"{response_schema_id=} should match {expected_schema_id=}")

    if not ((admitted_job_ref := start_response["job_ref"]) == job_ref):
        raise ResponseRejected(f"{admitted_job_ref=} should match {job_ref=}")

    if not ((admitted_analysis_kind_ref := start_response["analysis_kind_ref"]) == analysis_kind_ref):
        raise ResponseRejected(f"{admitted_analysis_kind_ref=} should match {analysis_kind_ref=}")

    if not ((admitted_provider_ref := start_response["provider_ref"]) == provider_ref):
        raise ResponseRejected(f"{admitted_provider_ref=} should match {provider_ref=}")

    try:
        require_attempt_ref(start_response["execution_attempt_ref"])
    except (TypeError, ValueError) as cause:
        raise ResponseRejected(str(cause)) from None

    if not ((admitted_attempt_state := start_response["state"]) in ("in_progress", "succeeded", "failed", "expired")):
        raise ResponseRejected(f"{admitted_attempt_state=} should be 'in_progress', 'succeeded', 'failed' or 'expired'")

    if not (type((start_replayed := start_response["replayed"])) is (expected_type := bool)):
        raise ResponseRejected(f"{type(start_replayed)=} ≠ {expected_type=}")

    timestamp = read_timestamp(start_response["started_at"])

    return AttemptAdmission(
        start_response["execution_attempt_ref"],
        job_ref,
        analysis_kind_ref,
        provider_ref,
        admitted_attempt_state,
        timestamp,
        start_replayed,
    )


def read_progress(raw, *, execution_attempt_ref, phase, condition_code):
    """Bind the acknowledged snapshot to the requested observation.

    Pinned API provider/schemas/execution_attempt_progress_response.v1.schema.json.
    Repeating stored facts may succeed after terminalization; updated_at remains
    their original observation time, not proof of continuing work or availability.
    """

    progress_response = read_document(raw, ATTEMPT_RESPONSE_BYTES)

    if not (type(progress_response) is (expected_type := dict)):
        raise ResponseRejected(f"{type(progress_response)=} ≠ {expected_type=}")

    response_fields = set(progress_response)

    expected_fields = {
        "schema_id",
        "execution_attempt_ref",
        "phase",
        "condition_code",
        "updated_at",
    }

    if not (response_fields == expected_fields):
        raise ResponseRejected(f"{response_fields=} should match {expected_fields=}")

    expected_schema_id = "nmr.provider.execution_attempt_progress_response.v1"

    if not ((response_schema_id := progress_response["schema_id"]) == expected_schema_id):
        raise ResponseRejected(f"{response_schema_id=} should match {expected_schema_id=}")

    if not ((acknowledged_execution_attempt_ref := progress_response["execution_attempt_ref"]) == execution_attempt_ref):
        raise ResponseRejected(f"{acknowledged_execution_attempt_ref=} should match {execution_attempt_ref=}")

    if not ((acknowledged_phase := progress_response["phase"]) == phase):
        raise ResponseRejected(f"{acknowledged_phase=} should match {phase=}")

    if not ((acknowledged_condition_code := progress_response["condition_code"]) == condition_code):
        raise ResponseRejected(f"acknowledged_condition_code should match {condition_code=}")

    return AttemptProgress(execution_attempt_ref, phase, condition_code, read_timestamp(progress_response["updated_at"]))


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
    outcome_response = read_document(raw, ATTEMPT_RESPONSE_BYTES)

    if not (type(outcome_response) is (expected_type := dict)):
        raise ResponseRejected(f"{type(outcome_response)=} ≠ {expected_type=}")

    response_fields = set(outcome_response)
    expected_fields = {"schema_id", "execution_attempt_ref", "committed_at", "replayed", *fields}

    if not (response_fields == expected_fields):
        raise ResponseRejected(f"{response_fields=} should match {expected_fields=}")

    expected_schema_id = f"nmr.provider.execution_attempt_{operation}_response.v1"

    if not ((response_schema_id := outcome_response["schema_id"]) == expected_schema_id):
        raise ResponseRejected(f"{response_schema_id=} should match {expected_schema_id=}")

    if not ((acknowledged_execution_attempt_ref := outcome_response["execution_attempt_ref"]) == execution_attempt_ref):
        raise ResponseRejected(f"{acknowledged_execution_attempt_ref=} should match {execution_attempt_ref=}")

    if not (type((outcome_replayed := outcome_response["replayed"])) is (expected_type := bool)):
        raise ResponseRejected(f"{type(outcome_replayed)=} ≠ {expected_type=}")

    return outcome_response


def read_complete(raw, *, execution_attempt_ref, result_schema_id, result):
    completion_response = _outcome_response(
        raw,
        "complete",
        execution_attempt_ref,
        ("analysis_result_ref", "result_schema_id", "result_fingerprint", "result_byte_length"),
    )

    result_fingerprint = "sha256:" + sha256(result).hexdigest()
    submitted_result_byte_length = len(result)

    if not (completion_response["result_schema_id"] == result_schema_id):
        raise ResponseRejected("Completion response result_schema_id should match the retained result schema")

    if not ((acknowledged_result_fingerprint := completion_response["result_fingerprint"]) == result_fingerprint):
        raise ResponseRejected(f"{acknowledged_result_fingerprint=} should match {result_fingerprint=}")

    if not (type((acknowledged_result_byte_length := completion_response["result_byte_length"])) is (expected_type := int)):
        raise ResponseRejected(f"{type(acknowledged_result_byte_length)=} ≠ {expected_type=}")

    if not (acknowledged_result_byte_length == submitted_result_byte_length):
        raise ResponseRejected(f"{acknowledged_result_byte_length=} should match {submitted_result_byte_length=}")

    result_ref = completion_response["analysis_result_ref"]

    if not (type(result_ref) is (expected_type := str)):
        raise ResponseRejected(f"{type(result_ref)=} ≠ {expected_type=}")

    if re.fullmatch(r"analysis_result:sha256:[0-9a-f]{64}", result_ref) is None:
        raise ResponseRejected("Analysis result reference must be analysis_result:sha256: followed by 64 lowercase hexadecimal digits")

    return AttemptCompletion(
        execution_attempt_ref,
        result_ref,
        result_schema_id,
        result_fingerprint,
        submitted_result_byte_length,
        read_timestamp(completion_response["committed_at"]),
        completion_response["replayed"],
    )


def read_fail(raw, *, execution_attempt_ref, failure_code, failure_message):
    failure_response = _outcome_response(raw, "fail", execution_attempt_ref, ("failure_code", "failure_message"))

    if not (failure_response["failure_code"] == failure_code):
        raise ResponseRejected("Failure response failure_code should match the retained failure code")

    if not (failure_response["failure_message"] == failure_message):
        raise ResponseRejected("Failure response failure_message should match the published explanation")

    return AttemptFailure(
        execution_attempt_ref,
        failure_code,
        failure_message,
        read_timestamp(failure_response["committed_at"]),
        failure_response["replayed"]
    )
