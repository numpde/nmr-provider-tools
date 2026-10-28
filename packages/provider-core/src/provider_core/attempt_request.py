"""Own Attempt request admission and preserve retained start/outcome bytes.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/execution_attempt_start_request.v1.schema.json and
provider/routes.py::EXECUTION_ATTEMPT_START_ROUTE. The lifecycle owner retains the
exact body before first dispatch. This module neither generates keys nor stores
request state.
"""

from provider_core.container_guard import require_container

require_container()

import binascii
import json
import re
from base64 import b64decode, b64encode

from provider_core.attempt_response import require_attempt_ref
from provider_core.response_json import ResponseRejected, read_document
from provider_core.upload_response import require_job_ref


def require_provider_attempt_key(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is None:
        raise ValueError(
            "Provider attempt key must contain 1 through 128 ASCII letters, digits, dots, underscores, colons or hyphens"
        )


def start_body(job_ref, provider_attempt_key):
    """Encode explicit start facts once for durable retention and exact replay."""
    require_job_ref(job_ref)
    require_provider_attempt_key(provider_attempt_key)
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_start_request.v1",
            "job_ref": job_ref,
            "provider_attempt_key": provider_attempt_key,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def require_start_body(body, *, job_ref):
    """Check retained content; never replace it with a newly serialized document."""
    try:
        document = read_document(body, 4096)
    except ResponseRejected as cause:
        raise ValueError(f"Cannot start Execution Attempt: retained request is invalid: {cause}") from None
    if type(document) is not dict or set(document) != {"schema_id", "job_ref", "provider_attempt_key"}:
        raise ValueError("Cannot start Execution Attempt: retained request fields do not match the start schema")
    if document["schema_id"] != "nmr.provider.execution_attempt_start_request.v1" or document["job_ref"] != job_ref:
        raise ValueError("Cannot start Execution Attempt: retained request schema or Job differs from the expected start")
    require_provider_attempt_key(document["provider_attempt_key"])


def require_analysis_kind(value):
    if not (type(value) is str and len(value) <= 128 and re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) is not None):
        raise ValueError("Analysis kind must be a lower snake-case catalog key of at most 128 characters")


def progress_body(phase, condition_code):
    """Encode the caller's current observation, not a historical replay queue.

    Pinned API provider/schemas/execution_attempt_progress_request.v1.schema.json.
    Composition must supply a public condition code, never derive one from raw
    exception/input/endpoint text. Grammar checks cannot establish that provenance.
    """
    if phase not in ("preparing", "running"):
        raise ValueError("Cannot report Attempt progress: phase must be preparing or running")
    if condition_code is not None and (
        type(condition_code) is not str
        or len(condition_code) > 128
        or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", condition_code) is None
    ):
        raise ValueError(
            "Cannot report Attempt progress: condition code must be null or a lower snake-case public code of at most 128 characters"
        )
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_progress_request.v1",
            "phase": phase,
            "condition_code": condition_code,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def require_provider_ref(value):
    if type(value) is not str or re.fullmatch(r"provider:[A-Za-z0-9_.-]{1,119}", value) is None:
        raise ValueError(
            "Provider reference must be provider: followed by 1 through 119 letters, digits, underscores, dots or hyphens"
        )


def _scalar_text(value, label):
    if type(value) is not str or not 1 <= len(value) <= 1024 or any(c == "\x00" or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f"Cannot publish Attempt outcome: {label} must contain 1 through 1024 Unicode scalar values without NUL")


def _failure_code(value):
    if not (type(value) is str and len(value) <= 128 and re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) is not None):
        raise ValueError("Cannot publish Attempt failure: code must be lower snake-case ASCII of at most 128 characters")


def complete_body(execution_attempt_ref, result_schema_id, result):
    """Encode opaque result bytes once for exact terminal delivery and replay.

    Authority: pinned provider/schemas/execution_attempt_complete_request.v1.schema.json
    and operations/canonical_content.py. Core does not interpret scientific content.
    """
    require_attempt_ref(execution_attempt_ref)
    _scalar_text(result_schema_id, "result schema identity")
    if type(result) is not bytes or not 1 <= len(result) <= 786432:
        raise ValueError("Cannot publish Attempt result: content must be immutable bytes with length 1 through 786432")
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_complete_request.v1",
            "execution_attempt_ref": execution_attempt_ref,
            "result_schema_id": result_schema_id,
            "canonical_result_base64": b64encode(result).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def fail_body(execution_attempt_ref, failure_code, failure_message):
    """Encode the consumer's publishable explanation without truncation or cleanup.

    Pinned provider/schemas/execution_attempt_fail_request.v1.schema.json allows
    useful producer explanations. Composition must protect credentials/confidential
    material before this call; text-shape validation is not secret detection.
    """
    require_attempt_ref(execution_attempt_ref)
    _failure_code(failure_code)
    _scalar_text(failure_message, "failure message")
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_fail_request.v1",
            "execution_attempt_ref": execution_attempt_ref,
            "failure_code": failure_code,
            "failure_message": failure_message,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _outcome_document(body, operation, limit, execution_attempt_ref, fields):
    try:
        document = read_document(body, limit)
    except ResponseRejected as cause:
        raise ValueError(f"Cannot publish Attempt outcome: retained request is invalid: {cause}") from None
    if type(document) is not dict or set(document) != {"schema_id", "execution_attempt_ref", *fields}:
        raise ValueError("Cannot publish Attempt outcome: retained fields do not match the terminal request schema")
    if (
        document["schema_id"] != f"nmr.provider.execution_attempt_{operation}_request.v1"
        or document["execution_attempt_ref"] != execution_attempt_ref
    ):
        raise ValueError("Cannot publish Attempt outcome: retained schema or Attempt differs from the intended publication")
    return document


def read_complete_body(body, *, execution_attempt_ref):
    """Validate retained completion without changing the bytes to be sent."""
    document = _outcome_document(
        body, "complete", 2097152, execution_attempt_ref, ("result_schema_id", "canonical_result_base64")
    )
    _scalar_text(document["result_schema_id"], "result schema identity")
    encoded = document["canonical_result_base64"]
    if type(encoded) is not str or not 1 <= len(encoded) <= 1048576:
        raise ValueError("Cannot publish Attempt result: base64 content must contain 1 through 1048576 characters")
    try:
        result = b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Cannot publish Attempt result: content is not valid standard base64") from None
    if b64encode(result).decode("ascii") != encoded or not 1 <= len(result) <= 786432:
        raise ValueError("Cannot publish Attempt result: base64 must be canonical and encode 1 through 786432 bytes")
    return document["result_schema_id"], result


def read_fail_body(body, *, execution_attempt_ref):
    """Admit exact public failure facts; do not normalize their text."""
    document = _outcome_document(body, "fail", 16384, execution_attempt_ref, ("failure_code", "failure_message"))
    _failure_code(document["failure_code"])
    _scalar_text(document["failure_message"], "failure message")
    return document["failure_code"], document["failure_message"]
