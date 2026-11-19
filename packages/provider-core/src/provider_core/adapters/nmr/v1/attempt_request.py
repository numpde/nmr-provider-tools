"""Encode admitted Attempt facts once for exact delivery and retained replay.

The lifecycle owner retains the resulting bytes before dispatch. These encoders
do not generate keys, interpret scientific results or authorize another send.
"""

from provider_core.container_guard import require_container

require_container()

import json
from base64 import b64encode

from provider_core.validation.upstream.nmr.v1.attempt_request import (
    require_complete_inputs,
    require_fail_inputs,
    require_progress_inputs,
    require_start_inputs,
)


def start_body(job_ref, provider_attempt_key):
    """Encode explicit start facts once for durable retention and exact replay."""

    require_start_inputs(job_ref, provider_attempt_key)

    return (
        json
        .dumps(
            {
                "schema_id": "nmr.provider.execution_attempt_start_request.v1",
                "job_ref": job_ref,
                "provider_attempt_key": provider_attempt_key,
            },
            separators=(",", ":"),
        )
        .encode("utf-8")
    )


def progress_body(phase, condition_code):
    """Encode the caller's current observation, not a historical replay queue."""

    require_progress_inputs(phase, condition_code)

    return (
        json
        .dumps(
            {
                "schema_id": "nmr.provider.execution_attempt_progress_request.v1",
                "phase": phase,
                "condition_code": condition_code,
            },
            separators=(",", ":"),
        )
        .encode("utf-8")
    )


def complete_body(execution_attempt_ref, result_schema_id, result):
    """Encode opaque result bytes once for exact terminal delivery and replay."""

    require_complete_inputs(execution_attempt_ref, result_schema_id, result)

    return (
        json
        .dumps(
            {
                "schema_id": "nmr.provider.execution_attempt_complete_request.v1",
                "execution_attempt_ref": execution_attempt_ref,
                "result_schema_id": result_schema_id,
                "canonical_result_base64": b64encode(result).decode("ascii"),
            },
            separators=(",", ":"),
        )
        .encode("utf-8")
    )


def fail_body(execution_attempt_ref, failure_code, failure_message):
    """Encode the supplied failure explanation without truncation or cleanup."""

    require_fail_inputs(execution_attempt_ref, failure_code, failure_message)

    return (
        json
        .dumps(
            {
                "schema_id": "nmr.provider.execution_attempt_fail_request.v1",
                "execution_attempt_ref": execution_attempt_ref,
                "failure_code": failure_code,
                "failure_message": failure_message,
            },
            separators=(",", ":"),
        )
        .encode("utf-8")
    )
