"""Job-page examples from pinned API jobs_list_response.v1.schema.json.

Wire revision: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8.
Input identities reuse the same synthetic bytes as the input-read fixture.
"""

import json
from dataclasses import asdict

from tests.fakes.job_input import facts


def job():
    return {**asdict(facts()), "analysis_kind_ref": "nmr_predict", "created_at": "2026-10-18T00:00:00.123456Z"}


def page():
    return {"schema_id": "nmr.provider.jobs.list.response.v1", "analysis_kind_ref": "nmr_predict",
            "has_provider_execution_attempt": False, "jobs": [job()], "next_cursor": "YQ"}


def response():
    return json.dumps(page()).encode()
