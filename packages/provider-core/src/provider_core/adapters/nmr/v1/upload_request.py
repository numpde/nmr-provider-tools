"""Render Provider API Upload metadata targets, separate from store byte transfer."""

from provider_core.container_guard import require_container

require_container()


def membership_route(job_ref: str) -> tuple[str, str]:
    """Render the membership target from the caller's admitted Job identity."""

    return ("GET", f"/provider/v1/jobs/{job_ref}/uploads")


def read_grant_route(job_ref: str, upload_ref: str) -> tuple[str, str]:
    """Render fresh issuance for the exact admitted Job and Upload binding."""

    return ("POST", f"/provider/v1/jobs/{job_ref}/uploads/{upload_ref}/read-capability")
