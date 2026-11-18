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

from provider_core.attempt_request import require_analysis_kind
from provider_core.job_input import FeedInputFacts, JobInputRejected
from provider_core.paging import MAX_PAGE_ITEMS, paging_query, require_cursor
from provider_core.response_json import ResponseRejected, read_document, read_timestamp
from provider_core.type_requirements import require_type

# The pinned discovery response owns this page byte ceiling.
# repo-check: allow-module-constant
PAGE_RESPONSE_BYTES = 65536


@dataclass(frozen=True, slots=True)
class DiscoveredJob:
    input: FeedInputFacts
    analysis_kind_ref: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JobPage:
    analysis_kind_ref: str
    has_provider_execution_attempt: bool
    jobs: tuple[DiscoveredJob, ...]
    next_cursor: str | None = field(repr=False)


def jobs_query(*, analysis_kind_ref, has_provider_execution_attempt, limit, cursor):
    """Encode explicit selection choices once for the signed request target."""
    require_analysis_kind(analysis_kind_ref)

    require_type(has_provider_execution_attempt, bool)

    # silent-defaults: allow-contract -- Encode the caller-selected boolean as the protocol-required lowercase wire value.
    view = "true" if has_provider_execution_attempt else "false"

    query = f"analysis_kind_ref={analysis_kind_ref}&has_provider_execution_attempt={view}&"

    return query.encode("ascii") + paging_query(limit=limit, cursor=cursor)


def read_jobs_page(raw, *, analysis_kind_ref, has_provider_execution_attempt, limit):
    """Bind a complete page to admitted request facts; preserve server ordering."""
    document = read_document(raw, PAGE_RESPONSE_BYTES)

    if not (type(document) is (expected_type := dict)):
        raise ResponseRejected(f"{type(document)=} ≠ {expected_type=}")

    if (set(document) != {
        "schema_id",
        "analysis_kind_ref",
        "has_provider_execution_attempt",
        "jobs",
        "next_cursor",
    }):
        raise ResponseRejected("Job discovery response fields do not match the supported page schema")

    if document["schema_id"] != "nmr.provider.jobs.list.response.v1":
        raise ResponseRejected("Job discovery response schema is not the supported version")

    if document["analysis_kind_ref"] != analysis_kind_ref:
        raise ResponseRejected("Job discovery analysis kind or provider-attempt view differs from the request")

    if not (type((document_has_provider_execution_attempt := document["has_provider_execution_attempt"])) is (expected_type := bool)):
        raise ResponseRejected(f"{type(document_has_provider_execution_attempt)=} ≠ {expected_type=}")

    if document["has_provider_execution_attempt"] != has_provider_execution_attempt:
        raise ResponseRejected("Job discovery analysis kind or provider-attempt view differs from the request")

    jobs = document["jobs"]

    if not (type(jobs) is (expected_type := list)):
        raise ResponseRejected(f"{type(jobs)=} ≠ {expected_type=}")

    if len(jobs) > MAX_PAGE_ITEMS:
        raise ResponseRejected("Job discovery page exceeds the requested or supported item count")

    if len(jobs) > limit:
        raise ResponseRejected("Job discovery page exceeds the requested or supported item count")

    try:
        require_cursor(document["next_cursor"])
    except (TypeError, ValueError) as cause:
        raise ResponseRejected(str(cause)) from None

    return JobPage(
        analysis_kind_ref,
        has_provider_execution_attempt,
                   # repo-check: allow-tuple-generator -- The admitted JobPage retains server order in one immutable collection.
        tuple(_read_job(job, analysis_kind_ref) for job in jobs),
        document["next_cursor"]
    )


def _read_job(job, analysis_kind_ref):
    if not (type(job) is (expected_type := dict)):
        raise ResponseRejected(f"{type(job)=} ≠ {expected_type=}")

    if (set(job) != {
        "job_ref",
        "analysis_kind_ref",
        "input_fingerprint",
        "input_schema_id",
        "input_byte_length",
        "created_at",
    }):
        raise ResponseRejected("Discovered Job fields do not match the supported item schema")

    if job["analysis_kind_ref"] != analysis_kind_ref:
        raise ResponseRejected("Discovered Job analysis kind differs from the requested page")

    try:
        identity = FeedInputFacts(job["job_ref"], job["input_schema_id"], job["input_fingerprint"], job["input_byte_length"])
    except JobInputRejected as cause:
        raise ResponseRejected(str(cause)) from None

    return DiscoveredJob(identity, analysis_kind_ref, read_timestamp(job["created_at"]))
