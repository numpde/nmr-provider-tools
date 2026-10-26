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
    if type(document) is not dict or set(document) != {"schema_id", "attempts", "next_cursor"}:
        raise ResponseRejected("Attempt inventory response fields do not match the supported page schema")
    if document["schema_id"] != "nmr.provider.execution_attempts.list.response.v1":
        raise ResponseRejected("Attempt inventory response schema is not the supported version")
    attempts = document["attempts"]
    if type(attempts) is not list or len(attempts) > MAX_PAGE_ITEMS or len(attempts) > limit:
        raise ResponseRejected("Attempt inventory exceeds the requested or supported item count")
    try:
        require_cursor(document["next_cursor"])
    except ValueError as cause:
        raise ResponseRejected(str(cause)) from None
    # repo-check: allow-tuple-generator -- The admitted AttemptPage retains an immutable ordered collection.
    return AttemptInventoryPage(tuple(_read_item(item) for item in attempts), document["next_cursor"])


def _read_item(item):
    if type(item) is not dict or set(item) != {
        "analysis_kind_ref", "execution_attempt_ref", "job_ref", "provider_attempt_key", "state", "started_at",
    }:
        raise ResponseRejected("Inventory Attempt fields do not match the supported item schema")
    try:
        require_analysis_kind(item["analysis_kind_ref"])
        require_attempt_ref(item["execution_attempt_ref"])
        require_job_ref(item["job_ref"])
        require_provider_attempt_key(item["provider_attempt_key"])
    except ValueError as cause:
        raise ResponseRejected(str(cause)) from None
    if item["state"] != "in_progress":
        raise ResponseRejected("Inventory Attempt state must be in_progress")
    return InventoryAttempt(item["analysis_kind_ref"], item["execution_attempt_ref"], item["job_ref"],
                            item["provider_attempt_key"], item["state"], read_timestamp(item["started_at"]))
