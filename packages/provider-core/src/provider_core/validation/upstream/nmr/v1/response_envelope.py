"""Admit routed API headers and reviewed problem meanings without operation state.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
security/problems.py, edge_topology.py and request_ids.py. Optional unreadable
problem details establish no reviewed meaning; the adapter retains the HTTP reply.
"""

from provider_core.container_guard import require_container

require_container()

from provider_core.response_json import ResponseRejected, read_document
from provider_core.validation.upstream.nmr.v1.request_ids import read_request_id


# Reviewed local descriptions interpret only the selected public problem meanings
# repo-check: allow-module-constant
_PROBLEM_REASONS = {
    (
        409,
        "operation-conflict",
        "execution_attempt_outcome_expired",
    ): "the Attempt expired before this outcome could be accepted; this send made no change; retain prior publication evidence",
    (
        409,
        "operation-conflict",
        "execution_attempt_completion_after_failure",
    ): "the Attempt already has a failure outcome; this completion made no change; do not rerun the analysis to resolve publication",
    (
        409,
        "operation-conflict",
        "execution_attempt_failure_after_success",
    ): "the Attempt already has a successful outcome; this failure publication made no change; retain the original outcome",
    (
        409,
        "operation-conflict",
        "execution_attempt_completion_replay_mismatch",
    ): "the submitted result differs from the recorded completion; this send made no change; reconcile using the original retained publication",
    (
        409,
        "operation-conflict",
        "execution_attempt_failure_replay_mismatch",
    ): "the submitted failure differs from the recorded failure; this send made no change; reconcile using the original retained publication",
    (
        409,
        "operation-conflict",
        "execution_attempt_progress_terminal",
    ): "the Attempt is already terminal and cannot accept changed progress; this update changed neither progress nor outcome; do not retry it or restart the analysis",
    (
        409,
        "operation-conflict",
        "execution_attempt_progress_regression",
    ): "the Attempt already has running progress and cannot return to preparing; this update did not change progress; check progress reporting instead of retrying it or restarting the analysis",
    (
        401,
        "authentication-failed",
        "authentication_failed",
    ): "API could not authenticate the request; check the configured provider credential",
    (401, "authentication-failed", "authentication_nonce_reused"): "API rejected a reused signature nonce",
    (
        401,
        "authentication-failed",
        "authentication_window_closed",
    ): "the API's request-acceptance window advanced; a fresh signature is required",
    (
        401,
        "authentication-failed",
        "signature_expired",
    ): "the request signature expired; check the provider clock and request delay",
    (
        401,
        "authentication-failed",
        "signature_created_in_future",
    ): "the signature timestamp is in the future; check the provider clock",
    (403, "authorization-denied", "authorization_denied"): "API denied this provider access to the requested resource; check the provider's API permissions",
    (404, "not-found", "resource_not_found"): "the requested resource was not found or is not accessible to this provider",
    (408, "request-body-timeout", "request_body_timeout"): "the server did not receive the complete request body in time, so this send did not run the operation; any earlier unanswered sends remain unresolved; check the API client's retry rule for this operation before retrying with a fresh signature",
    (
        409,
        "operation-conflict",
        "upload_read_not_finalized",
    ): "the Upload is not finalized; ask its owner to check and finalize the existing Upload before requesting access; do not reupload solely because of this error",
    (
        409,
        "operation-conflict",
        "project_purge_in_progress",
    ): "Project removal has started; another request cannot restore access; ask the Job owner or administrator about another authorized source",
    (503, "service-unavailable", "service_unavailable"): "API service is unavailable; no verified response was obtained",
    (
        409,
        "operation-conflict",
        "provider_attempt_key_conflict",
    ): "the retained attempt key conflicts with an existing start; this send made no change",
    (
        409,
        "operation-conflict",
        "job_provider_attempt_limit_reached",
    ): "this provider has reached the Job's attempt limit; this send made no change",
    (409, "operation-conflict", "job_attempt_limit_reached"): "the Job has reached its attempt limit; this send made no change",
}


def read_problem(response):
    """Return a reviewed code/meaning pair, or no meaning for unadmitted details.

    The original response remains evidence even when optional diagnostics fail
    admission. No operation, retry decision or effect certainty is owned here.
    """

    content_types = [value for (key, value) in response.headers if key.lower() == b"content-type"]

    if content_types != [b"application/problem+json"]:
        return None

    try:
        problem = read_document(response.body, 4096)
    # silent-defaults: allow-exception-outcome -- Unreadable optional diagnostics establish no reviewed problem meaning
    except ResponseRejected:
        return None

    if (type(problem) is not dict) or (set(problem) != {"type", "title", "status", "instance", "request_id", "code", "detail"}):
        return None

    if (type(problem["status"]) is not int) or (problem["status"] != response.status):
        return None

    if any((type(problem[name]) is not str) for name in ("type", "title", "instance", "request_id", "code", "detail")):
        return None

    request_id = read_request_id(response.headers)

    if (request_id is None) or (problem["request_id"] != request_id):
        return None

    reason = _PROBLEM_REASONS.get((response.status, problem["type"].removeprefix("urn:nmr-api:problem:"), problem["code"]))

    if (not problem["type"].startswith("urn:nmr-api:problem:")) or (reason is None):
        return None

    return (problem["code"], reason)


def is_api_success(status: int | None) -> bool:
    """Recognize the selected Provider API's successful response status."""

    return status == 200


def require_headers(response, expected_topology):
    """Admit the routed API environment before body or problem interpretation."""

    # silent-defaults: allow-contract -- The pinned protocol selects JSON for success and problem JSON for refusal.
    content_type = b"application/json" if is_api_success(response.status) else b"application/problem+json"

    for (name, expected) in ((b"content-type", content_type), (b"cache-control", b"no-store")):
        values = [value for (key, value) in response.headers if key.lower() == name]

        if values != [expected]:
            raise ResponseRejected(f"HTTP {response.status=} response must contain one {name.decode()=} header with value {expected.decode()=}")

    if any((name.lower() == b"content-encoding") for (name, _) in response.headers):
        raise ResponseRejected("encoded response bodies are not supported by the API contract")

    markers = [value for (name, value) in response.headers if name.lower() == b"nmr-api-topology"]

    if markers != [expected_topology.encode("ascii")]:
        raise ResponseRejected("response must identify exactly the configured Nmr-Api-Topology")

    request_ids = [value for (name, value) in response.headers if name.lower() == b"x-request-id"]

    if (len(request_ids) > 1) or ((not is_api_success(response.status)) and (read_request_id(response.headers) is None)):
        raise ResponseRejected("response request correlation is missing or ambiguous")
