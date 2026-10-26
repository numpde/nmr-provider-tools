"""Synthetic inventory from the pinned API's execution_attempts_list schema.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/execution_attempts_list_response.v1.schema.json.
These observations do not represent a retained local start or execution claim.
"""

import json

from tests.fakes.attempts import ATTEMPT_REF


def attempt(**changes):
    return {"analysis_kind_ref": "nmr_predict", "execution_attempt_ref": ATTEMPT_REF,
            "job_ref": "job:example", "provider_attempt_key": "retained-start:01",
            "state": "in_progress", "started_at": "2026-10-18T00:00:00.123456Z"} | changes


def page(**changes):
    return {"schema_id": "nmr.provider.execution_attempts.list.response.v1",
            "attempts": [attempt()], "next_cursor": "YQ"} | changes


def response(**changes):
    return json.dumps(page(**changes)).encode()
