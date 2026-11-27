"""Admit one authenticated discovery page, without selecting or starting work.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
provider/routes.py:324-346, provider/schemas/jobs_list_response.v1.schema.json,
provider/jobs_list_request.py and operations/provider_job_feed_service.py:32-106.
The false view lists selected open Jobs without this provider's prior Attempt.
The true view means any prior Attempt, not an inventory of active work. Neither
view grants execution. Cursors stay opaque; only their wire encoding is checked.
"""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass, field
from datetime import datetime

from provider_core.validation.upstream.nmr.v1.attempt_request import require_analysis_kind
from provider_core.validation.upstream.nmr.v1.job_input import FeedInputFacts, JobInputRejected
from provider_core.validation.upstream.nmr.v1.paging import MAX_PAGE_ITEMS, require_cursor
from provider_core.response_json import ResponseRejected, read_document
from provider_core.validation.upstream.nmr.v1.timestamps import read_timestamp
from provider_core.type_requirements import require_type

# The pinned discovery response owns this page byte ceiling.
# repo-check: allow-module-constant
PAGE_RESPONSE_BYTES = 65536


@dataclass(frozen=True, slots=True)
class DiscoveredJob:
    input: FeedInputFacts
    analysis_kind_ref: str  # repo-check: allow-identifier-type -- API offering name bound to the selected feed by _read_job.
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JobPage:
    analysis_kind_ref: str  # repo-check: allow-identifier-type -- API offering name bound to the selected feed by read_jobs_page.
    has_provider_execution_attempt: bool
    jobs: tuple[DiscoveredJob, ...]
    next_cursor: str | None = field(repr=False)


def require_jobs_query_inputs(*, analysis_kind_ref, has_provider_execution_attempt):
    """Admit an analysis-kind filter and explicit prior-Attempt view."""

    require_analysis_kind(analysis_kind_ref)

    require_type(has_provider_execution_attempt, bool)


def read_jobs_page(raw, *, analysis_kind_ref, has_provider_execution_attempt, limit):
    """Bind a complete page to admitted request facts; preserve server ordering."""
    jobs_response = read_document(raw, PAGE_RESPONSE_BYTES)

    require_type(jobs_response, dict, error_factory=ResponseRejected)

    response_fields = set(jobs_response)

    expected_fields = {
        "schema_id",
        "analysis_kind_ref",
        "has_provider_execution_attempt",
        "jobs",
        "next_cursor",
    }

    if not (response_fields == expected_fields):
        raise ResponseRejected(f"{response_fields=} should match {expected_fields=}")

    expected_schema_id = "nmr.provider.jobs.list.response.v1"

    if not ((response_schema_id := jobs_response["schema_id"]) == expected_schema_id):
        raise ResponseRejected(f"{response_schema_id=} should match {expected_schema_id=}")

    if not ((observed_analysis_kind_ref := jobs_response["analysis_kind_ref"]) == analysis_kind_ref):
        raise ResponseRejected(f"{observed_analysis_kind_ref=} should match {analysis_kind_ref=}")

    require_type((observed_has_provider_execution_attempt := jobs_response["has_provider_execution_attempt"]), bool, error_factory=ResponseRejected)

    if not (observed_has_provider_execution_attempt == has_provider_execution_attempt):
        raise ResponseRejected(f"{observed_has_provider_execution_attempt=} should match {has_provider_execution_attempt=}")

    jobs = jobs_response["jobs"]

    require_type(jobs, list, error_factory=ResponseRejected)

    job_count = len(jobs)

    if not (job_count <= MAX_PAGE_ITEMS):
        raise ResponseRejected(f"{job_count=} should be at most {MAX_PAGE_ITEMS=}")

    if not (job_count <= limit):
        raise ResponseRejected(f"{job_count=} should be at most {limit=}")

    try:
        require_cursor(jobs_response["next_cursor"])
    except (TypeError, ValueError) as cause:
        raise ResponseRejected(str(cause)) from cause

    return JobPage(
        analysis_kind_ref,
        has_provider_execution_attempt,
        # repo-check: allow-tuple-generator -- The admitted JobPage retains server order in one immutable collection.
        tuple(_read_job(job, analysis_kind_ref) for job in jobs),
        jobs_response["next_cursor"]
    )


def _read_job(job, analysis_kind_ref):
    require_type(job, dict, error_factory=ResponseRejected)

    job_fields = set(job)

    expected_fields = {
        "job_ref",
        "analysis_kind_ref",
        "input_fingerprint",
        "input_schema_id",
        "input_byte_length",
        "created_at",
    }

    if not (job_fields == expected_fields):
        raise ResponseRejected(f"{job_fields=} should match {expected_fields=}")

    if not ((observed_analysis_kind_ref := job["analysis_kind_ref"]) == analysis_kind_ref):
        raise ResponseRejected(f"{observed_analysis_kind_ref=} should match {analysis_kind_ref=}")

    try:
        identity = FeedInputFacts(job["job_ref"], job["input_schema_id"], job["input_fingerprint"], job["input_byte_length"])
    except JobInputRejected as cause:
        raise ResponseRejected(str(cause)) from cause

    return DiscoveredJob(identity, analysis_kind_ref, read_timestamp(job["created_at"]))
