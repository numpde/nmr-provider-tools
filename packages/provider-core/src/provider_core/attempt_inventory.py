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
    if not (state == "in_progress"):
        raise ValueError(f"{state=} should be 'in_progress'")

    return b"state=in_progress&" + paging_query(limit=limit, cursor=cursor)


def read_attempt_inventory(raw, *, limit):
    """Admit all rows before exposing a page; preserve the server's ordering."""
    inventory_response = read_document(raw, ATTEMPT_RESPONSE_BYTES)

    if not (type(inventory_response) is (expected_type := dict)):
        raise ResponseRejected(f"{type(inventory_response)=} ≠ {expected_type=}")

    response_fields = set(inventory_response)
    expected_fields = {"schema_id", "attempts", "next_cursor"}

    if not (response_fields == expected_fields):
        raise ResponseRejected(f"{response_fields=} should match {expected_fields=}")

    expected_schema_id = "nmr.provider.execution_attempts.list.response.v1"

    if not ((response_schema_id := inventory_response["schema_id"]) == expected_schema_id):
        raise ResponseRejected(f"{response_schema_id=} should match {expected_schema_id=}")

    attempts = inventory_response["attempts"]

    if not (type(attempts) is (expected_type := list)):
        raise ResponseRejected(f"{type(attempts)=} ≠ {expected_type=}")

    attempt_count = len(attempts)

    if not (attempt_count <= MAX_PAGE_ITEMS):
        raise ResponseRejected(f"{attempt_count=} should be at most {MAX_PAGE_ITEMS=}")

    if not (attempt_count <= limit):
        raise ResponseRejected(f"{attempt_count=} should be at most {limit=}")

    try:
        require_cursor(inventory_response["next_cursor"])
    except (TypeError, ValueError) as cause:
        raise ResponseRejected(str(cause)) from cause

    # repo-check: allow-tuple-generator -- The admitted AttemptPage retains an immutable ordered collection.
    return AttemptInventoryPage(tuple(_read_attempt(attempt) for attempt in attempts), inventory_response["next_cursor"])


def _read_attempt(attempt):
    if not (type(attempt) is (expected_type := dict)):
        raise ResponseRejected(f"{type(attempt)=} ≠ {expected_type=}")

    attempt_fields = set(attempt)

    expected_fields = {
        "analysis_kind_ref",
        "execution_attempt_ref",
        "job_ref",
        "provider_attempt_key",
        "state",
        "started_at",
    }

    if not (attempt_fields == expected_fields):
        raise ResponseRejected(f"{attempt_fields=} should match {expected_fields=}")

    try:
        require_analysis_kind(attempt["analysis_kind_ref"])
        require_attempt_ref(attempt["execution_attempt_ref"])
        require_job_ref(attempt["job_ref"])
        require_provider_attempt_key(attempt["provider_attempt_key"])
    except (TypeError, ValueError) as cause:
        raise ResponseRejected(str(cause)) from cause

    if not ((inventory_attempt_state := attempt["state"]) == "in_progress"):
        raise ResponseRejected(f"{inventory_attempt_state=} should be 'in_progress'")

    return InventoryAttempt(
        attempt["analysis_kind_ref"],
        attempt["execution_attempt_ref"],
        attempt["job_ref"],
        attempt["provider_attempt_key"],
        attempt["state"],
        read_timestamp(attempt["started_at"])
    )
