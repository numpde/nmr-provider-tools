"""Admit Attempt inputs and retained requests without reconstructing sent bytes.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/execution_attempt_{start,progress,complete,fail}_request.v1.schema.json,
security/analysis_kind_refs.py and wire/schemas/nmr/. The lifecycle owner retains
the exact body before first dispatch. This module neither generates keys nor
stores request state.
"""

from provider_core.container_guard import require_container

require_container()

import re
from base64 import b64decode, b64encode

from provider_core.validation.upstream.nmr.v1.attempt_response import require_attempt_ref
from provider_core.response_json import read_document
from provider_core.validation.upstream.nmr.v1.job_refs import require_job_ref
from provider_core.type_requirements import require_type


# The retained COMPLETE request body ceiling also bounds delivery IPC intake
# repo-check: allow-module-constant
MAX_COMPLETE_REQUEST_BYTES = 2097152


def require_provider_attempt_key(provider_attempt_key):
    require_type(provider_attempt_key, str)

    key_character_count = len(provider_attempt_key)

    if not (key_character_count <= (max_key_characters := 128)):
        raise ValueError(f"{key_character_count=} should be at most {max_key_characters=}")

    if not re.fullmatch((expected_pattern := r"[A-Za-z0-9._:-]+"), provider_attempt_key):
        raise ValueError(f"{provider_attempt_key=} should match {expected_pattern=}")


def require_start_inputs(job_ref, provider_attempt_key):
    """Admit explicit start facts before the adapter encodes them."""

    require_job_ref(job_ref)
    require_provider_attempt_key(provider_attempt_key)


def require_start_body(body, *, job_ref):
    """Check retained content; never replace it with a newly serialized document."""

    start_request = read_document(body, 4096)

    require_type(start_request, dict)

    start_request_fields = set(start_request)
    expected_fields = {"schema_id", "job_ref", "provider_attempt_key"}

    if not (start_request_fields == expected_fields):
        raise ValueError(f"{start_request_fields=} should match {expected_fields=}")

    expected_schema_id = "nmr.provider.execution_attempt_start_request.v1"

    if not ((retained_schema_id := start_request["schema_id"]) == expected_schema_id):
        raise ValueError(f"{retained_schema_id=} should match {expected_schema_id=}")

    if not ((retained_job_ref := start_request["job_ref"]) == job_ref):
        raise ValueError(f"{retained_job_ref=} should match {job_ref=}")

    require_provider_attempt_key(start_request["provider_attempt_key"])


def require_analysis_kind(analysis_kind_ref):
    require_type(analysis_kind_ref, str)

    analysis_kind_character_count = len(analysis_kind_ref)

    if not (analysis_kind_character_count <= (max_analysis_kind_characters := 128)):
        raise ValueError(f"{analysis_kind_character_count=} should be at most {max_analysis_kind_characters=}")

    if not re.fullmatch((expected_pattern := r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"), analysis_kind_ref):
        raise ValueError(f"{analysis_kind_ref=} should match {expected_pattern=}")


def require_progress_inputs(phase, condition_code):
    """Admit the caller's current phase and optional public condition code.

    Pinned API provider/schemas/execution_attempt_progress_request.v1.schema.json.
    Composition must supply a public condition code, never derive one from raw
    exception/input/endpoint text. Grammar checks cannot establish that provenance.
    """

    if not (phase in ("preparing", "running")):
        raise ValueError(f"{phase=} should be 'preparing' or 'running'")

    if condition_code is not None:
        require_type(condition_code, str)

        condition_code_length = len(condition_code)

        if not (condition_code_length <= (max_condition_code_characters := 128)):
            raise ValueError(f"{condition_code_length=} should be at most {max_condition_code_characters=}")

        if not re.fullmatch((expected_pattern := r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"), condition_code):
            raise ValueError(f"{condition_code=} should match {expected_pattern=}")


def require_provider_ref(provider_ref):
    require_type(provider_ref, str)

    provider_ref_character_count = len(provider_ref)

    if not (provider_ref_character_count <= (max_provider_ref_characters := 128)):
        raise ValueError(f"{provider_ref_character_count=} should be at most {max_provider_ref_characters=}")

    if not re.fullmatch((expected_pattern := r"provider:[A-Za-z0-9_.-]+"), provider_ref):
        raise ValueError(f"{provider_ref=} should match {expected_pattern=}")


def _require_outcome_text(scalar_text, field_name):
    """Admit the v1 result-schema and failure-message Unicode scalar profile."""

    if not (type(scalar_text) is (expected_type := str)):
        raise TypeError(f"{field_name=}; {type(scalar_text)=} ≠ {expected_type=}")

    scalar_character_count = len(scalar_text)
    (min_scalar_characters, max_scalar_characters) = (1, 1024)

    if not (min_scalar_characters <= scalar_character_count <= max_scalar_characters):
        raise ValueError(f"{field_name=}; {scalar_character_count=} should be between {min_scalar_characters=} and {max_scalar_characters=}")

    if not all(((character != "\x00") and (not (0xD800 <= ord(character) <= 0xDFFF))) for character in scalar_text):
        raise ValueError(f"{field_name=}; {scalar_text=} should not contain NUL or surrogate code points")


def _failure_code(failure_code):
    require_type(failure_code, str)

    failure_code_character_count = len(failure_code)

    if not (failure_code_character_count <= (max_failure_code_characters := 128)):
        raise ValueError(f"{failure_code_character_count=} should be at most {max_failure_code_characters=}")

    if not re.fullmatch((expected_pattern := r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"), failure_code):
        raise ValueError(f"{failure_code=} should match {expected_pattern=}")


def require_complete_inputs(execution_attempt_ref, result_schema_id, result):
    """Admit the result's wire representation without interpreting its science.

    Authority: pinned provider/schemas/execution_attempt_complete_request.v1.schema.json
    and operations/canonical_content.py. Core does not interpret scientific content.
    """
    require_attempt_ref(execution_attempt_ref)
    _require_outcome_text(result_schema_id, "result schema identity")

    require_type(result, bytes)

    result_byte_length = len(result)
    (min_result_bytes, max_result_bytes) = (1, 786432)

    if not (min_result_bytes <= result_byte_length <= max_result_bytes):
        raise ValueError(f"{result_byte_length=} should be between {min_result_bytes=} and {max_result_bytes=}")


def require_fail_inputs(execution_attempt_ref, failure_code, failure_message):
    """Admit the consumer's publishable explanation without truncation or cleanup.

    Pinned provider/schemas/execution_attempt_fail_request.v1.schema.json allows
    useful producer explanations. Composition must protect credentials/confidential
    material before this call; text-shape validation is not secret detection.
    """
    require_attempt_ref(execution_attempt_ref)
    _failure_code(failure_code)
    _require_outcome_text(failure_message, "failure message")


def _outcome_document(body, operation, limit, execution_attempt_ref, fields):
    outcome_request = read_document(body, limit)

    require_type(outcome_request, dict)

    outcome_request_fields = set(outcome_request)
    expected_fields = {"schema_id", "execution_attempt_ref", *fields}

    if not (outcome_request_fields == expected_fields):
        raise ValueError(f"{outcome_request_fields=} should match {expected_fields=}")

    expected_schema_id = f"nmr.provider.execution_attempt_{operation}_request.v1"  # machine-string: request schema validation requires this exact NMR identifier

    if not ((retained_schema_id := outcome_request["schema_id"]) == expected_schema_id):
        raise ValueError(f"{retained_schema_id=} should match {expected_schema_id=}")

    if not ((retained_execution_attempt_ref := outcome_request["execution_attempt_ref"]) == execution_attempt_ref):
        raise ValueError(f"{retained_execution_attempt_ref=} should match {execution_attempt_ref=}")

    return outcome_request


def read_complete_body(body, *, execution_attempt_ref):
    """Validate retained completion without changing the bytes to be sent."""

    completion_request = _outcome_document(
        body,
        "complete",
        MAX_COMPLETE_REQUEST_BYTES,
        execution_attempt_ref,
        ("result_schema_id", "canonical_result_base64")
    )

    _require_outcome_text(completion_request["result_schema_id"], "result schema identity")
    retained_result_base64 = completion_request["canonical_result_base64"]

    require_type(retained_result_base64, str)

    retained_base64_character_count = len(retained_result_base64)
    (min_base64_characters, max_base64_characters) = (1, 1048576)

    if not (min_base64_characters <= retained_base64_character_count <= max_base64_characters):
        raise ValueError(f"{retained_base64_character_count=} should be between {min_base64_characters=} and {max_base64_characters=}")

    canonical_result_bytes = b64decode(retained_result_base64, validate=True)
    expected_result_base64 = b64encode(canonical_result_bytes).decode("ascii")

    if not (retained_result_base64 == expected_result_base64):
        raise ValueError(f"{retained_result_base64[-4:]=} should match {expected_result_base64[-4:]=}")

    result_byte_length = len(canonical_result_bytes)
    (min_result_bytes, max_result_bytes) = (1, 786432)

    if not (min_result_bytes <= result_byte_length <= max_result_bytes):
        raise ValueError(f"{result_byte_length=} should be between {min_result_bytes=} and {max_result_bytes=}")

    return (completion_request["result_schema_id"], canonical_result_bytes)


def read_fail_body(body, *, execution_attempt_ref):
    """Admit exact public failure facts; do not normalize their text."""
    failure_request = _outcome_document(body, "fail", 16384, execution_attempt_ref, ("failure_code", "failure_message"))

    _failure_code(failure_request["failure_code"])
    _require_outcome_text(failure_request["failure_message"], "failure message")

    return (failure_request["failure_code"], failure_request["failure_message"])
