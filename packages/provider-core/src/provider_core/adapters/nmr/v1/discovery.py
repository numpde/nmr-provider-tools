"""Encode explicit discovery filters without traversing or selecting work.

Keep query order and opaque cursor bytes stable for signing. The caller owns
whether to follow another page and what to do with admitted observations.
"""

from provider_core.container_guard import require_container

require_container()

from provider_core.validation.upstream.nmr.v1.attempt_inventory import require_inventory_state
from provider_core.validation.upstream.nmr.v1.job_feed import require_jobs_query_inputs
from provider_core.validation.upstream.nmr.v1.paging import require_page_inputs


def jobs_route() -> tuple[str, str]:
    """Declare the Job feed target independently of caller-selected query fields."""

    return ("GET", "/provider/v1/jobs")


def inventory_route() -> tuple[str, str]:
    """Declare the provider-wide Attempt inventory target."""

    return ("GET", "/provider/v1/execution-attempts")


def paging_query(*, limit, cursor):
    """Encode the admitted page limit and optional cursor in their signed order."""

    require_page_inputs(limit=limit, cursor=cursor)
    query = f"limit={limit}"

    if cursor is not None:
        query += "&cursor=" + cursor

    return query.encode("ascii")


def jobs_query(*, analysis_kind_ref, has_provider_execution_attempt, limit, cursor):
    """Encode explicit selection choices once for the signed request target."""

    require_jobs_query_inputs(
        analysis_kind_ref=analysis_kind_ref,
        has_provider_execution_attempt=has_provider_execution_attempt,
    )

    # silent-defaults: allow-contract -- Encode the caller-selected boolean as the protocol-required lowercase wire value.
    view = "true" if has_provider_execution_attempt else "false"

    query = f"analysis_kind_ref={analysis_kind_ref}&has_provider_execution_attempt={view}&"

    return query.encode("ascii") + paging_query(limit=limit, cursor=cursor)


def inventory_query(*, state, limit, cursor):
    """Encode the live inventory filter before its admitted paging fields."""

    require_inventory_state(state)

    return b"state=in_progress&" + paging_query(limit=limit, cursor=cursor)
