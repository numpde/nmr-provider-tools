"""Bind admitted response facts to the caller's operation and retained evidence."""

from provider_core.container_guard import require_container

require_container()

from provider_core.api_errors import ApiFailure
from provider_core.response_json import ResponseRejected
from provider_core.validation.upstream.nmr.v1.response_envelope import is_api_success, read_problem, require_headers


def http_failure(response, operation):
    """Keep the complete reply and expose only its admitted problem meaning."""

    problem = read_problem(response)

    if problem is None:
        return ApiFailure(operation, f"API returned HTTP {response.status=}; no verified response was obtained", response=response)

    (problem_code, problem_reason) = problem
    failure = ApiFailure(operation, f"{problem_reason=} (HTTP {response.status=})", response=response)
    failure.problem_code = problem_code

    return failure


def require_api_headers(response, operation, expected_topology):
    """Bind header rejection to complete or late response evidence."""

    try:
        require_headers(response, expected_topology)
    except ResponseRejected as rejection:
        raise ApiFailure(operation, str(rejection), response=response) from None


def require_api_envelope(response, operation, expected_topology):
    """Admit headers before interpreting a complete response's problem body."""

    require_api_headers(response, operation, expected_topology)

    if not is_api_success(response.status):
        raise http_failure(response, operation)
