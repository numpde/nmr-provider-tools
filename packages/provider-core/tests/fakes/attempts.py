"""Attempt read fixtures from the API revision cited in attempt_response.py."""

import json
from hashlib import sha256

ATTEMPT_REF = "execution_attempt:sha256:" + "a" * 64


def observation(**changes):
    document = {
        "schema_id": "nmr.provider.execution_attempt_read_response.v1",
        "execution_attempt_ref": ATTEMPT_REF,
        "job_ref": "job:example",
        "state": "in_progress",
        "job_state": "open",
    }
    document.update(changes)
    return json.dumps(document).encode()


def start_response(**changes):
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_start_response.v1",
            "execution_attempt_ref": ATTEMPT_REF,
            "job_ref": "job:example",
            "analysis_kind_ref": "nmr_predict",
            "provider_ref": "provider:example",
            "state": "in_progress",
            "started_at": "2026-10-18T00:00:00Z",
            "replayed": False,
        }
        | changes
    ).encode()


def retained_start(**changes):
    # Noncompact ordering/whitespace are intentional: recovery must resend bytes.
    return (
        json.dumps(
            {
                "provider_attempt_key": "retained-start:01",
                "job_ref": "job:example",
                "schema_id": "nmr.provider.execution_attempt_start_request.v1",
            }
            | changes,
            indent=2,
        ).encode()
        + b"\n"
    )


def progress_response(**changes):
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_progress_response.v1",
            "execution_attempt_ref": ATTEMPT_REF,
            "phase": "preparing",
            "condition_code": None,
            "updated_at": "2026-10-18T00:00:00.123456Z",
        }
        | changes
    ).encode()


def complete_response(**changes):
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_complete_response.v1",
            "execution_attempt_ref": ATTEMPT_REF,
            "analysis_result_ref": "analysis_result:sha256:" + "b" * 64,
            "result_schema_id": " result\n雪 ",
            "result_fingerprint": "sha256:" + sha256(b"result bytes").hexdigest(),
            "result_byte_length": len(b"result bytes"),
            "committed_at": "2026-10-18T00:00:00Z",
            "replayed": False,
        }
        | changes
    ).encode()


def fail_response(**changes):
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_fail_response.v1",
            "execution_attempt_ref": ATTEMPT_REF,
            "failure_code": "input_rejected",
            "failure_message": " Explain\n雪\x01 ",
            "committed_at": "2026-10-18T00:00:00Z",
            "replayed": False,
        }
        | changes
    ).encode()
