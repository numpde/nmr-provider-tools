"""Render the exact Job-input target and query from admitted caller facts."""

from provider_core.container_guard import require_container

require_container()


def job_input_route(job_ref: str) -> tuple[str, str]:
    """Render the input target without changing the admitted Job identity."""

    return (
        "GET",
        f"/provider/v1/jobs/{job_ref}/input"  # machine-string: the signed NMR API target uses this exact Job-input route
    )


def job_input_query(analysis_kind_ref: str) -> bytes:
    """Encode the already-admitted catalog key without HTTP query normalization."""

    return b"analysis_kind_ref=" + analysis_kind_ref.encode("ascii")
