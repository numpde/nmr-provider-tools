"""Admit one live in-progress Attempt page, without claiming or resuming work.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
provider/routes.py:350-365, provider/execution_attempts_list_request.py,
provider/schemas/execution_attempts_list_response.v1.schema.json and
operations/provider_execution_attempt_list_service.py:38-108. The authenticated
provider principal owns the view across analysis kinds, filtered to visible Jobs.
A page is no atomic
traversal snapshot; absence does not establish historical absence or completion.
"""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass, field
from datetime import datetime

from provider_core.attempt_request import require_analysis_kind, require_provider_attempt_key
from provider_core.attempt_response import ATTEMPT_RESPONSE_BYTES, require_attempt_ref
from provider_core.paging import MAX_PAGE_ITEMS, paging_query, require_cursor
from provider_core.response_json import ResponseRejected, read_document, read_timestamp
from provider_core.upload_response import require_job_ref


@dataclass(frozen=True, slots=True)
class InventoryAttempt:
    analysis_kind_ref: str
    execution_attempt_ref: str
    job_ref: str
    provider_attempt_key: str = field(repr=False)
    state: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptInventoryPage:
    attempts: tuple[InventoryAttempt, ...]
    next_cursor: str | None = field(repr=False)


def inventory_query(*, state, limit, cursor):
    if state != "in_progress":
        raise ValueError("Attempt inventory state must be in_progress")

    return b"state=in_progress&" + paging_query(limit=limit, cursor=cursor)


def read_attempt_inventory(raw, *, limit):
    """Admit all rows before exposing a page; preserve the server's ordering."""
    document = read_document(raw, ATTEMPT_RESPONSE_BYTES)

    if not (type(document) is (expected_type := dict)):
        raise ResponseRejected(f"{type(document)=} ≠ {expected_type=}")

    if set(document) != {"schema_id", "attempts", "next_cursor"}:
        raise ResponseRejected("Attempt inventory response fields do not match the supported page schema")

    if document["schema_id"] != "nmr.provider.execution_attempts.list.response.v1":
        raise ResponseRejected("Attempt inventory response schema is not the supported version")

    attempts = document["attempts"]

    if not (type(attempts) is (expected_type := list)):
        raise ResponseRejected(f"{type(attempts)=} ≠ {expected_type=}")

    if len(attempts) > MAX_PAGE_ITEMS:
        raise ResponseRejected("Attempt inventory exceeds the requested or supported item count")

    if len(attempts) > limit:
        raise ResponseRejected("Attempt inventory exceeds the requested or supported item count")

    try:
        require_cursor(document["next_cursor"])
    except (TypeError, ValueError) as cause:
        raise ResponseRejected(str(cause)) from None

    # repo-check: allow-tuple-generator -- The admitted AttemptPage retains an immutable ordered collection.
    return AttemptInventoryPage(tuple(_read_attempt(attempt) for attempt in attempts), document["next_cursor"])


def _read_attempt(attempt):
    if not (type(attempt) is (expected_type := dict)):
        raise ResponseRejected(f"{type(attempt)=} ≠ {expected_type=}")

    if set(attempt) != {
        "analysis_kind_ref",
        "execution_attempt_ref",
        "job_ref",
        "provider_attempt_key",
        "state",
        "started_at",
    }:
        raise ResponseRejected("Inventory Attempt fields do not match the supported item schema")

    try:
        require_analysis_kind(attempt["analysis_kind_ref"])
        require_attempt_ref(attempt["execution_attempt_ref"])
        require_job_ref(attempt["job_ref"])
        require_provider_attempt_key(attempt["provider_attempt_key"])
    except (TypeError, ValueError) as cause:
        raise ResponseRejected(str(cause)) from None

    if attempt["state"] != "in_progress":
        raise ResponseRejected("Inventory Attempt state must be in_progress")

    return InventoryAttempt(
        attempt["analysis_kind_ref"],
        attempt["execution_attempt_ref"],
        attempt["job_ref"],
        attempt["provider_attempt_key"],
        attempt["state"],
        read_timestamp(attempt["started_at"])
    )
