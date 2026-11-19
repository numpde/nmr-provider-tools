"""Admit Job references shared by the selected NMR v1 endpoints.

Authority: the pinned job_input_read_response and execution_attempt_start_request
v1 schemas. Endpoint readers retain their own response and identity checks.
"""

from provider_core.container_guard import require_container

require_container()

import re
import reprlib

from provider_core.type_requirements import require_type


# repo-check: allow-module-constant
JOB_REF_PATTERN = r"job:[A-Za-z0-9_.-]{1,124}"


def require_job_ref(job_ref: str) -> None:
    """Require the selected version's Job-reference type and spelling."""

    require_type(job_ref, str)

    if not re.fullmatch(JOB_REF_PATTERN, job_ref):
        raise ValueError(f"{reprlib.repr(job_ref)=} should match {JOB_REF_PATTERN=}")
