"""Own signed API operations; scientific admission remains with the consumer.

Wire authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/routes.py::JOB_INPUT_READ_ROUTE and
nmr_api/security/analysis_kind_refs.py. The response ceiling is the API's
131072-byte route contract, not an estimate from a typical input envelope.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import errno
import json
import math
import re
import secrets
import socket
import time
from builtins import ExceptionGroup
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpcore

from provider_core.attempt_inventory import inventory_query, read_attempt_inventory
from provider_core.attempt_request import (
    progress_body,
    read_complete_body,
    read_fail_body,
    require_analysis_kind,
    require_provider_ref,
    require_start_body,
)
from provider_core.attempt_response import (
    ATTEMPT_RESPONSE_BYTES,
    read_attempt,
    read_complete,
    read_fail,
    read_progress,
    read_start,
    require_attempt_ref,
)
from provider_core.hello import HELLO_RESPONSE_BYTES, hello_body, read_hello
from provider_core.job_feed import PAGE_RESPONSE_BYTES, jobs_query, read_jobs_page
from provider_core.job_input import MAX_RESPONSE_BYTES, FeedInputFacts, JobInputRejected, verify_job_input
from provider_core.response_json import ResponseRejected
from provider_core.signing import signed_request
from provider_core.transport import ExchangeFailed, response_request_id
from provider_core.upload_response import (
    GRANT_RESPONSE_BYTES,
    MEMBERSHIP_RESPONSE_BYTES,
    read_grant,
    read_membership,
    require_job_ref,
    require_upload_ref,
)


class ApiFailure(RuntimeError):
    """Safe operation context with private evidence for authorized diagnostics.

    response and cause can contain private instructions or credentials. They must
    not be rendered verbatim. This owner never retries an operation implicitly.
    """

    def __init__(self, operation, reason, *, response=None, cause=None):
        super().__init__(f"Cannot {operation}: {reason}")
        self.operation = operation
        self.response = response
        self.cause = cause
        self.problem_code = None
        self.request_method = None
        self.request_target = None
        self.issuance_may_have_committed = False
        self.attempt_mutation_may_have_committed = False

    def _received_head(self):
        # Transport retirement can add late headers to the original cause. Read
        # diagnostic facts from that owner; never copy them into stale API state.
        if self.response is not None:
            return self.response
        # silent-defaults: allow-contract -- Only a received transport head supplies response evidence; other failures leave it absent.
        return self.cause.response_head if isinstance(self.cause, ExchangeFailed) else None

    @property
    def status(self):
        head = self._received_head()
        # silent-defaults: allow-contract -- An unobserved response has no status; absence must remain distinguishable from a received status.
        return None if head is None else head.status

    @property
    def request_id(self):
        head = self._received_head()
        # silent-defaults: allow-contract -- Request correlation exists only in an observed response.
        return None if head is None else response_request_id(head.headers)

    @property
    def retry_after_seconds(self):
        """Read server timing advice; this does not authorize another request.

        RFC 9110 section 10.2.3 permits delay-seconds or an HTTP-date. Invalid
        or ambiguous advice leaves the operation owner's local pacing intact.
        """
        head = self._received_head()
        # silent-defaults: allow-contract -- Absent or ambiguous server timing advice grants no Retry-After floor.
        values = [] if head is None else [value for name, value in head.headers if name.lower() == b"retry-after"]
        if len(values) != 1:
            return None
        try:
            value = values[0].decode("ascii").strip()
            if re.fullmatch(r"[0-9]+", value):
                seconds = float(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    # The obsolete asctime HTTP-date form also denotes GMT.
                    date = date.replace(tzinfo=timezone.utc)
                seconds = max(0.0, date.timestamp() - time.time())
        except (ValueError, OverflowError, TypeError):
            return None
        # silent-defaults: allow-contract -- Nonfinite server timing advice is unavailable; local retry pacing remains authoritative.
        return seconds if math.isfinite(seconds) else None


def require_api_topology(value):
    """Admit the edge vocabulary, independently of credential deployment profiles.

    API pin above, nmr_api/edge_topology.py::EdgeTopology. In particular, the
    credential profile 'run' is not the response topology 'web'. No default.
    """
    if type(value) is not str or value not in {"dev-local", "dev", "web"}:
        raise ValueError("Cannot configure Provider API: expected topology must be dev-local, dev or web")
    return value


# Reviewed subset of security/problems.py::PUBLIC_PROBLEM_CODES at the API
# revision above. Local text avoids disclosing arbitrary server detail/title.
# Reviewed local problem descriptions are the only received-code text exposed by this owner.
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


def _unique_problem_fields(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("Duplicate problem field")
        document[key] = value
    return document


def _reject_problem_constant(_value):
    raise ValueError("Nonfinite problem number")


def _http_failure(response, operation):
    """Expose only reviewed problem meanings; retain unknown replies privately.

    RFC 9457 envelope and 4096-byte bound: pinned API security/problems.py.
    Request correlation syntax: request_ids.py (1..128 visible ASCII bytes).
    This reads diagnostics, not mutation-effect certainty or retry permission.
    """
    failure = ApiFailure(operation, f"API returned HTTP {response.status}; no verified response was obtained", response=response)
    content_types = [value for key, value in response.headers if key.lower() == b"content-type"]
    if content_types != [b"application/problem+json"] or len(response.body) > 4096:
        return failure
    try:
        problem = json.loads(
            response.body.decode("utf-8"), object_pairs_hook=_unique_problem_fields, parse_constant=_reject_problem_constant
        )
    # silent-defaults: allow-exception-outcome -- Unreadable optional diagnostics leave the original API failure unchanged
    except (ValueError, RecursionError):
        # The original response remains evidence; malformed diagnostics must not
        # replace the HTTP failure with a parser error or leak its private text.
        return failure
    if type(problem) is not dict or set(problem) != {"type", "title", "status", "instance", "request_id", "code", "detail"}:
        return failure
    if type(problem["status"]) is not int or problem["status"] != response.status:
        return failure
    if any(type(problem[name]) is not str for name in ("type", "title", "instance", "request_id", "code", "detail")):
        return failure
    if failure.request_id is None or problem["request_id"] != failure.request_id:
        return failure
    reason = _PROBLEM_REASONS.get((response.status, problem["type"].removeprefix("urn:nmr-api:problem:"), problem["code"]))
    if not problem["type"].startswith("urn:nmr-api:problem:") or reason is None:
        return failure
    failure = ApiFailure(operation, f"{reason} (HTTP {response.status})", response=response)
    failure.problem_code = problem["code"]
    return failure


def _require_api_headers(response, operation, expected_topology):
    """Admit the routed API environment before body or problem interpretation.

    API nmr_api/edge_topology.py and entrypoints/_edge_runtime.py; adapts SECS
    7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    src/secs_inference/provider/http.py:290-332. Chat/store remain neutral.
    """
    # silent-defaults: allow-contract -- The pinned protocol selects JSON for success and problem JSON for refusal.
    content_type = b"application/json" if response.status == 200 else b"application/problem+json"
    for name, expected in ((b"content-type", content_type), (b"cache-control", b"no-store")):
        values = [value for key, value in response.headers if key.lower() == name]
        if values != [expected]:
            raise ApiFailure(
                operation,
                f"HTTP {response.status} response must contain one {name.decode()} header with value {expected.decode()}",
                response=response,
            )
    if any(name.lower() == b"content-encoding" for name, _ in response.headers):
        raise ApiFailure(operation, "encoded response bodies are not supported by the API contract", response=response)
    markers = [value for name, value in response.headers if name.lower() == b"nmr-api-topology"]
    if markers != [expected_topology.encode("ascii")]:
        raise ApiFailure(operation, "response must identify exactly the configured Nmr-Api-Topology", response=response)
    request_ids = [value for name, value in response.headers if name.lower() == b"x-request-id"]
    if len(request_ids) > 1 or (response.status != 200 and response_request_id(response.headers) is None):
        raise ApiFailure(operation, "response request correlation is missing or ambiguous", response=response)


def _require_api_envelope(response, operation, expected_topology):
    """Admit headers before interpreting a complete response's problem body."""
    _require_api_headers(response, operation, expected_topology)
    if response.status != 200:
        raise _http_failure(response, operation)


def _temporary_io_failure(cause, remaining):
    """Recognize bounded native/httpcore cause chains, never their error text.

    httpcore1.0.9 _exceptions.map_exceptions retains the native cause. AnyIO can
    group failed address attempts beneath OSError; every leaf must be temporary.
    Unknown wrappers, TLS/protocol defects and excessive nesting remain fatal.
    """
    if remaining == 0:
        return False
    if type(cause) in (TimeoutError, httpcore.ConnectTimeout, httpcore.ReadTimeout, httpcore.WriteTimeout):
        return True
    if type(cause) is socket.gaierror:
        return cause.errno == socket.EAI_AGAIN
    if type(cause) is ExceptionGroup:
        return all(_temporary_io_failure(child, remaining - 1) for child in cause.exceptions)
    if type(cause) in (OSError, ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        if cause.errno in {
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.ETIMEDOUT,
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.EPIPE,
        }:
            return True
        if cause.errno is not None:
            return False
    elif type(cause) not in (httpcore.ConnectError, httpcore.ReadError, httpcore.WriteError):
        return False
    return _temporary_io_failure(cause.__cause__, remaining - 1)


class ProviderAPI:
    """One execution owner's authenticated operations, with retained failure context.

    Composition owns the transport lifetime and awaits its retirement. On caller
    cancellation, last_failure preserves the operation and protected transport
    evidence while CancelledError still reaches the task owner.
    """

    def __init__(self, identity, transport, *, expected_topology):
        self._expected_topology = require_api_topology(expected_topology)
        self._identity = identity
        self._transport = transport
        self.last_failure = None

    @property
    def origin(self):
        return self._identity.origin

    @property
    def identity(self):
        """The granted signing capability, for trusted composition's binding checks."""
        return self._identity

    @property
    def expected_topology(self):
        """The configured routed API environment, independent of signing profile."""
        return self._expected_topology

    @property
    def exchange_seconds(self):
        return self._transport.exchange_seconds

    @property
    def retirement_seconds(self):
        return self._transport.retirement_seconds

    @property
    def retirement_confirmed(self):
        return self._transport.retirement_confirmed

    @property
    def pending_owner(self):
        return self._transport.pending_owner

    def can_retry_read(self, failure):
        """Permit another safe GET after reviewed failure and confirmed retirement.

        Rate limits and HTTP500/502/503/504 permit a paced new read, without
        establishing transience or API authority. Verified request-body timeout
        and timing refusals permit fresh signing under the caller's policy.
        Keep request-ID evidence for operator diagnosis; an explicit wrong
        environment still refuses. Pinned OpenAPI gives all provider GETs the
        same read recovery contract (fixture source 12a53e720fc87e1a4144c76203f8853d5afaf753).
        Consult after retirement: it can reveal late evidence. Mutations never
        acquire retry permission here, including Hello and capability issuance.
        """
        return (
            (self._retryable_failure(failure) or self._settled_status_failure(failure, 500)
             or self._settled_status_failure(failure, 429))
            and failure.request_method == "GET"
        )

    def can_retry_job_discovery(self, failure):
        """Permit paced discovery reads while provider access may recover.

        Only this API's settled Jobs GET qualifies. Correlated authentication
        and authorization refusals can persist; permission does not promise
        recovery. The consumer owns stop checks, pacing and selection, and no
        returned page grants scientific execution.
        """
        if (
            type(failure) is not ApiFailure or failure is not self.last_failure or not self.retirement_confirmed
            or failure.request_method != "GET" or failure.request_target is None
            or failure.request_target.partition(b"?")[0] != b"/provider/v1/jobs"
        ):
            return False
        return self.can_retry_read(failure) or self._access_refusal(failure)

    def can_refresh_hello(self, failure):
        """Permit a freshly constructed current Hello, never replay an old body.

        Only this API's settled POST to the exact Hello target can qualify. A
        later receipt confirms the new announcement, not the earlier send.
        Other mutations retain their own replay/reconciliation requirements.
        Pinned OpenAPI's latest_hello recovery permits HTTP500 and a verified
        request-body timeout; the caller still constructs its current complete
        announcement with fresh signing. A settled rate limit likewise permits
        a paced current announcement, without promising service recovery.
        """
        return (
            (self._retryable_failure(failure) or self._settled_status_failure(failure, 500)
             or self._settled_status_failure(failure, 429))
            and failure.request_method == "POST"
            and failure.request_target == b"/provider/v1/hello"
        )

    def can_refresh_signature(self, failure):
        """Admit a settled no-effect request refusal for existing retry owners.

        This alone grants no operation replay. The caller must use its existing
        route/body permission, request deadlines and retry/stop policy, and retain
        the refusal. Other authentication failures do not qualify for signature
        refresh through this predicate. The
        complete correlated problem was admitted by _http_failure; an incomplete
        response or unsettled exchange cannot authorize this decision.

        Authority: nmr-api-v1 e7790cf6a922c39261fe736d5a23d17a9a5ea322,
        nmr_api/security/problems.py:826-841 (ReplayAdmissionExpired and
        signature_expired): the operation did not run; retry with a new signature.
        Pinned OpenAPI's request_body_timeout408 has the same fresh-signing rule;
        security/problems.py::RequestBodyTimeout establishes no domain change.
        This describes this send, never an earlier unacknowledged request.
        """
        return (
            type(failure) is ApiFailure
            and failure is self.last_failure
            and self.retirement_confirmed
            and failure.response is not None
            and (
                failure.status == 401 and failure.problem_code in {"authentication_window_closed", "signature_expired"}
                or failure.status == 408 and failure.problem_code == "request_body_timeout"
            )
        )

    def can_replay_start(self, failure):
        """Permit unchanged durable START replay after settled send uncertainty.

        The starter must retain the original Job, provider key and body, and
        admit the returned current state before execution. This predicate grants
        neither a new key nor a scientific rerun. Authority: pinned OpenAPI's
        /x-nmr-attempt-recovery/start, START x-nmr-recovery and HTTP500 response;
        source 12a53e720fc87e1a4144c76203f8853d5afaf753 (fixture manifest).
        """
        return (
            self._recoverable_attempt_failure(failure)
            and failure.request_method == "POST"
            and failure.request_target == b"/provider/v1/execution-attempts/start"
        )

    def can_defer_phase_progress(self, failure, *, execution_attempt_ref):
        """Admit settled send evidence for consumer-owned phase continuation.

        The executor must establish phase-only use; this failure records the
        route, not condition contents. It must retain uncertainty and obtain a
        fresh allowed observation before effects. No replay or delivery receipt
        is granted here. Authority: pinned OpenAPI's
        progress PUT x-nmr-recovery (latest_progress) and HTTP500 description;
        source 12a53e720fc87e1a4144c76203f8853d5afaf753 (fixture manifest).
        """
        return self._recoverable_progress_failure(failure, execution_attempt_ref)

    def can_retry_condition(self, failure, *, execution_attempt_ref):
        """Permit the same still-current awaited condition after settled failure.

        AnalysisSession owns sequential reporting; its pending phase/code must
        remain current until a matching acknowledgement. This route predicate
        cannot establish condition contents. It grants no scientific replay or
        phase-only deferral. Authority: the same pinned latest_progress recovery
        and HTTP500 rule documented by can_defer_phase_progress.
        """
        return self._recoverable_progress_failure(failure, execution_attempt_ref)

    def _recoverable_progress_failure(self, failure, execution_attempt_ref):
        require_attempt_ref(execution_attempt_ref)
        return (
            self._recoverable_attempt_failure(failure)
            and failure.request_method == "PUT"
            and failure.request_target == f"/provider/v1/execution-attempts/{execution_attempt_ref}/progress".encode("ascii")
        )

    def can_replay_terminal(self, failure):
        """Permit exact durable COMPLETE/FAIL replay after settled send uncertainty.

        The publisher must still own the unchanged retained command. Unlike a
        fresh GET or Hello, terminal replay is safe even when the first send
        committed. A pre-head protocol failure supplies no verified response;
        it does not establish EOF, transience or absence of server effects.
        Rate limits and reviewed authentication/authorization refusals permit
        paced attempts of the same command without claiming access will recover.

        Authority: pinned tests/fixtures/nmr_api_v1/openapi/openapi.v1.json,
        /x-nmr-attempt-recovery/terminal and each terminal POST's x-nmr-recovery;
        source 12a53e720fc87e1a4144c76203f8853d5afaf753 (fixture manifest).
        """
        if type(failure) is not ApiFailure or failure is not self.last_failure or not self.retirement_confirmed:
            return False
        if failure.request_method != "POST" or failure.request_target not in (
            b"/provider/v1/execution-attempts/complete",
            b"/provider/v1/execution-attempts/fail",
        ):
            return False
        if self._recoverable_attempt_failure(failure):
            return True
        return self._settled_status_failure(failure, 429) or self._access_refusal(failure)

    def _settled_status_failure(self, failure, status):
        """Recognize one settled origin/edge status, not operation permission."""
        if type(failure) is not ApiFailure or failure is not self.last_failure or not self.retirement_confirmed:
            return False
        head = failure._received_head()
        if head is not None and head.status == status and self._gateway_environment_matches(head):
            # Error/rate replies may come from an edge with a non-API body.
            # An interrupted body still needs settled, clean transport evidence.
            exchange = failure.cause
            return failure.response is not None or (
                type(exchange) is ExchangeFailed and not exchange.cleanup_failures
                and not isinstance(exchange.interruption, asyncio.CancelledError)
                and _temporary_io_failure(exchange.primary, 8)
            )
        return False

    def _access_refusal(self, failure):
        """Classify complete correlated refusals after the caller admits ownership."""
        # These refusals can persist. Each operation owns whether another
        # request is safe; this evidence does not establish absence of effects.
        return failure.response is not None and (
            failure.status == 401 and failure.problem_code in {
                "authentication_failed", "authentication_nonce_reused", "authentication_window_closed",
                "signature_expired", "signature_created_in_future",
            }
            or failure.status == 403 and failure.problem_code == "authorization_denied"
        )

    def _recoverable_attempt_failure(self, failure):
        """Share settled send evidence; public methods own exact route permission."""
        if type(failure) is not ApiFailure or failure is not self.last_failure or not self.retirement_confirmed:
            return False
        if self._retryable_failure(failure) or self._settled_status_failure(failure, 500):
            return True
        # Pinned Attempt recovery admits HTTP500 under its exact retained
        # request and operation-specific replay rules.
        if failure.response is not None:
            return False
        exchange = failure.cause
        if (
            type(exchange) is not ExchangeFailed
            or exchange.cleanup_failures
            or isinstance(exchange.interruption, asyncio.CancelledError)
        ):
            return False
        return exchange.response_head is None and type(exchange.primary) is httpcore.RemoteProtocolError

    def _retryable_failure(self, failure):
        """Share settled failure evidence, not operation retry authority."""
        if type(failure) is not ApiFailure or failure is not self.last_failure or not self.retirement_confirmed:
            return False
        if failure.response is not None:
            return self.can_refresh_signature(failure) or self._retryable_gateway(failure.response)
        exchange = failure.cause
        if (
            type(exchange) is not ExchangeFailed
            or exchange.cleanup_failures
            or isinstance(exchange.interruption, asyncio.CancelledError)
        ):
            return False
        if exchange.response_head is not None:
            if exchange.response_head.status in (502, 503, 504):
                return self._retryable_gateway(exchange.response_head) and _temporary_io_failure(exchange.primary, 8)
            try:
                _require_api_headers(exchange.response_head, failure.operation, self._expected_topology)
            # silent-defaults: allow-exception-outcome -- Unadmitted response headers grant no read retry
            except ApiFailure:
                return False
            if exchange.response_head.status != 200:
                return False
        if type(exchange.primary) is httpcore.ProxyError:
            # CONNECT gateway failures are availability evidence, not origin
            # responses or proof their underlying DNS/dial cause is temporary.
            # Missing status and policy refusal grant no retry permission;
            # successful CONNECT cannot authorize retry of a later TLS failure.
            return exchange.proxy_connect_status in (502, 503, 504)
        return _temporary_io_failure(exchange.primary, 8)

    def _retryable_gateway(self, response):
        return response.status in (502, 503, 504) and self._gateway_environment_matches(response)

    def _gateway_environment_matches(self, response):
        # A proxy may omit API headers or use HTML during maintenance. Retrying
        # an operation grants no interpretation of that body; an explicit environment
        # mismatch must not be hidden by the gateway status.
        markers = [value for name, value in response.headers if name.lower() == b"nmr-api-topology"]
        return markers in ([], [self._expected_topology.encode("ascii")])

    async def retire(self, *, owner=None):
        """Settle this API owner's transport; no domain mutation is retried."""
        await self._transport.retire(owner=owner)

    async def publish_hello(self, *, provider_ref, display_name, description, analysis_offerings, owner=None):
        """Publish one current complete snapshot; never replay a lost acknowledgement.

        Acceptance records an announcement, not scientific readiness. On failure,
        composition decides whether its latest snapshot is still intended before
        another send. The original transport and private failure remain owned here.
        """
        require_provider_ref(provider_ref)
        body = hello_body(display_name=display_name, description=description, analysis_offerings=analysis_offerings)
        operation = "confirm provider Hello acceptance"
        try:
            response = await self._exchange(
                operation,
                method="POST",
                path="/provider/v1/hello",
                query=b"",
                body=body,
                maximum_bytes=HELLO_RESPONSE_BYTES,
                owner=owner,
            )
            try:
                return read_hello(response.body, provider_ref=provider_ref)
            except ResponseRejected as cause:
                raise self._remember_failure(ApiFailure(operation, str(cause), response=response, cause=cause)) from None
        except (ApiFailure, asyncio.CancelledError):
            # This effect differs from Attempt replay and grant issuance. Keep
            # their flags unchanged while preserving Hello's uncertainty safely.
            self.last_failure.add_note(
                "Hello acceptance is unconfirmed; the directory snapshot may already have changed. "
                "No retry was attempted. Send only the current complete announcement if still intended."
            )
            raise

    async def jobs(self, *, analysis_kind_ref, has_provider_execution_attempt, limit, cursor, owner=None):
        """Read one discovery page; composition owns selection and pagination."""
        query = jobs_query(
            analysis_kind_ref=analysis_kind_ref,
            has_provider_execution_attempt=has_provider_execution_attempt,
            limit=limit,
            cursor=cursor,
        )
        response = await self._exchange(
            "list Jobs", owner=owner, method="GET", path="/provider/v1/jobs", query=query, maximum_bytes=PAGE_RESPONSE_BYTES
        )
        try:
            return read_jobs_page(
                response.body,
                analysis_kind_ref=analysis_kind_ref,
                has_provider_execution_attempt=has_provider_execution_attempt,
                limit=limit,
            )
        except ResponseRejected as cause:
            raise self._remember_failure(ApiFailure("list Jobs", str(cause), response=response, cause=cause)) from None

    async def execution_attempts(self, *, state, limit, cursor, owner=None):
        """Read one provider-wide inventory page; composition owns reconciliation."""
        query = inventory_query(state=state, limit=limit, cursor=cursor)
        response = await self._exchange(
            "list in-progress Attempts",
            owner=owner,
            method="GET",
            path="/provider/v1/execution-attempts",
            query=query,
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
        )
        try:
            return read_attempt_inventory(response.body, limit=limit)
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("list in-progress Attempts", str(cause), response=response, cause=cause)
            ) from None

    async def job_input(self, feed, *, analysis_kind_ref, owner=None):
        """Read once and bind bytes to feed facts authenticated by the caller.

        FeedInputFacts validates shape, not provenance. Composition must obtain
        these observations from its authenticated feed before calling this method.
        """
        if type(feed) is not FeedInputFacts:
            raise ValueError("Cannot read Job input: admitted feed identity is required")
        if (
            not (type(analysis_kind_ref) is str and len(analysis_kind_ref) <= 128 and re.fullmatch('[a-z][a-z0-9]*(?:_[a-z0-9]+)*', analysis_kind_ref) is not None)
        ):
            raise ValueError(
                "Cannot read Job input: analysis kind must be a lower snake-case catalog key of at most 128 characters"
            )
        response = await self._exchange(
            "read Job input",
            owner=owner,
            method="GET",
            path=f"/provider/v1/jobs/{feed.job_ref}/input",
            query=b"analysis_kind_ref=" + analysis_kind_ref.encode("ascii"),
            maximum_bytes=MAX_RESPONSE_BYTES,
        )
        try:
            return verify_job_input(response.body, feed=feed)
        except JobInputRejected as cause:
            raise self._remember_failure(ApiFailure("read Job input", str(cause), response=response, cause=cause)) from None

    async def uploads(self, job_ref):
        """Observe current membership once; no historical or cached authority."""
        require_job_ref(job_ref)
        response = await self._exchange(
            "read Job Upload membership",
            method="GET",
            path=f"/provider/v1/jobs/{job_ref}/uploads",
            query=b"",
            maximum_bytes=MEMBERSHIP_RESPONSE_BYTES,
        )
        try:
            return read_membership(response.body, job_ref=job_ref)
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("read Job Upload membership", str(cause), response=response, cause=cause)
            ) from None

    async def read_grant(self, job_ref, upload_ref):
        """Request fresh access once; a lost response can follow committed issuance.

        Pinned API provider/routes.py::JOB_UPLOAD_READ_CAPABILITY_ROUTE and
        operations/provider_job_upload_read_capability.py establish this effect:
        issuance can extend retention before a failure reply. Repeating this
        operation issues a new grant, not recovery of the previous bearer.
        """
        require_job_ref(job_ref)
        require_upload_ref(upload_ref)
        response = await self._exchange(
            "issue Upload read grant",
            method="POST",
            path=f"/provider/v1/jobs/{job_ref}/uploads/{upload_ref}/read-capability",
            query=b"",
            maximum_bytes=GRANT_RESPONSE_BYTES,
            issuance=True,
        )
        try:
            return read_grant(response.body, job_ref=job_ref, upload_ref=upload_ref)
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("issue Upload read grant", str(cause), response=response, cause=cause), issuance=True
            ) from None

    async def read_attempt(self, execution_attempt_ref, *, job_ref, owner=None):
        """Observe both states for the locally owned Attempt and Job.

        The expected Job binds readback to the caller's retained obligation.
        Neither state proves that scientific work ran or that particular result
        bytes committed; recovery must retain its own execution/publication facts.
        """
        require_attempt_ref(execution_attempt_ref)
        require_job_ref(job_ref)
        response = await self._exchange(
            "read Execution Attempt",
            owner=owner,
            method="GET",
            path=f"/provider/v1/execution-attempts/{execution_attempt_ref}",
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
        )
        try:
            return read_attempt(response.body, execution_attempt_ref=execution_attempt_ref, job_ref=job_ref)
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("read Execution Attempt", str(cause), response=response, cause=cause)
            ) from None

    async def start_attempt(self, body, *, job_ref, analysis_kind_ref, provider_ref, owner=None):
        """Send retained START bytes once; retries must reuse those exact bytes.

        Composition retains the body before dispatch and the received admission before
        scientific work. A replay may return a terminal Attempt. Admission alone
        never authorizes execution or resolves local execution-entry uncertainty.
        """
        require_job_ref(job_ref)
        require_analysis_kind(analysis_kind_ref)
        require_provider_ref(provider_ref)
        require_start_body(body, job_ref=job_ref)
        response = await self._exchange(
            "start Execution Attempt",
            method="POST",
            path="/provider/v1/execution-attempts/start",
            owner=owner,
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )
        try:
            return read_start(response.body, job_ref=job_ref, analysis_kind_ref=analysis_kind_ref, provider_ref=provider_ref)
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("start Execution Attempt", str(cause), response=response, cause=cause), attempt_mutation=True
            ) from None

    async def progress_attempt(self, execution_attempt_ref, *, phase, condition_code, owner=None):
        """Publish current facts once; never replay obsolete progress automatically.

        The lifecycle owner decides whether these facts still describe its latest
        observation. Neither preparing nor running proves model work or liveness.
        Public condition codes must come from the consumer's reviewed vocabulary.
        """
        require_attempt_ref(execution_attempt_ref)
        body = progress_body(phase, condition_code)
        response = await self._exchange(
            "report Attempt progress",
            owner=owner,
            method="PUT",
            path=f"/provider/v1/execution-attempts/{execution_attempt_ref}/progress",
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )
        try:
            return read_progress(
                response.body, execution_attempt_ref=execution_attempt_ref, phase=phase, condition_code=condition_code
            )
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("report Attempt progress", str(cause), response=response, cause=cause), attempt_mutation=True
            ) from None

    async def complete_attempt(self, body, *, execution_attempt_ref, owner=None):
        """Publish or reconcile exact retained result bytes without rerunning work.

        The caller retains this terminal choice and exact body for any permitted replay.
        Uncertainty never authorizes replacing it with another result or failure.
        """
        require_attempt_ref(execution_attempt_ref)
        result_schema_id, result = read_complete_body(body, execution_attempt_ref=execution_attempt_ref)
        response = await self._exchange(
            "complete Execution Attempt",
            method="POST",
            path="/provider/v1/execution-attempts/complete",
            owner=owner,
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )
        try:
            return read_complete(
                response.body, execution_attempt_ref=execution_attempt_ref, result_schema_id=result_schema_id, result=result
            )
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("complete Execution Attempt", str(cause), response=response, cause=cause), attempt_mutation=True
            ) from None

    async def fail_attempt(self, body, *, execution_attempt_ref, owner=None):
        """Publish or reconcile the retained public failure, never a replacement.

        Composition owns publishable text and private evidence retention. Core
        preserves exact accepted text; it does not guess which content is secret.
        """
        require_attempt_ref(execution_attempt_ref)
        failure_code, failure_message = read_fail_body(body, execution_attempt_ref=execution_attempt_ref)
        response = await self._exchange(
            "publish Attempt failure",
            method="POST",
            path="/provider/v1/execution-attempts/fail",
            owner=owner,
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )
        try:
            return read_fail(
                response.body,
                execution_attempt_ref=execution_attempt_ref,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("publish Attempt failure", str(cause), response=response, cause=cause), attempt_mutation=True
            ) from None

    # silent-defaults: allow-contract -- Only the exchange owner supplies a known signed method/target for operation-specific recovery; parser failures grant no replay authority.
    def _remember_failure(self, failure, *, issuance=False, attempt_mutation=False, request_method=None, request_target=None):
        failure.request_method = request_method
        failure.request_target = request_target
        # Reviewed 401 and request-body timeout meanings establish no domain
        # change for this send (security/problems.py).
        # These two grant-specific 409 branches precede issuance in operations/
        # provider_job_upload_read_capability.py; errors.py explicitly promises
        # no new link. Generic 403/404/409 statuses make no such assertion.
        no_domain_change = failure.problem_code is not None and (
            failure.status == 401
            or (failure.status == 408 and failure.problem_code == "request_body_timeout")
        )
        refused = no_domain_change or (
            failure.status == 409 and failure.problem_code in {"upload_read_not_finalized", "project_purge_in_progress"}
        )
        if issuance and not refused:
            failure.issuance_may_have_committed = True
            failure.add_note(
                "Download access may already have been issued and extended Upload retention; no retry was attempted."
            )
        # security/problems.py explicitly establishes no domain change for these
        # start refusals. operations/errors.py also explicitly establishes no
        # progress change for the two progress conflicts. These facts describe
        # this send, never an earlier lost response.
        attempt_refused = no_domain_change or (
            failure.status == 409 and failure.problem_code in {
                "provider_attempt_key_conflict",
                "job_provider_attempt_limit_reached",
                "job_attempt_limit_reached",
                "execution_attempt_progress_terminal",
                "execution_attempt_progress_regression",
                "execution_attempt_outcome_expired",
                "execution_attempt_completion_after_failure",
                "execution_attempt_failure_after_success",
                "execution_attempt_completion_replay_mismatch",
                "execution_attempt_failure_replay_mismatch",
            }
        )
        if attempt_mutation and not attempt_refused:
            failure.attempt_mutation_may_have_committed = True
            failure.add_note(
                "The Attempt change may already have committed; retain the original request for reconciliation. No retry was attempted."
            )
        self.last_failure = failure
        return failure

    # silent-defaults: allow-contract -- Callers explicitly mark issuance and Attempt effects; ordinary exchanges acquire neither effect claim.
    async def _exchange(
        self, operation, *, method, path, query, maximum_bytes, issuance=False, body=None, attempt_mutation=False, owner=None
    ):
        request = signed_request(
            self._identity,
            method=method,
            path=path,
            query=query,
            body=body,
            created=int(time.time()),
            nonce=secrets.token_bytes(16),
        )
        self.last_failure = None
        try:
            response = await self._transport.exchange(request, maximum_bytes=maximum_bytes, owner=owner)
        except asyncio.CancelledError:
            self._remember_failure(
                ApiFailure(
                    operation,
                    "request cancelled; transport retirement must be confirmed before reuse",
                    cause=self._transport.last_failure,
                ),
                issuance=issuance,
                attempt_mutation=attempt_mutation,
                request_method=method,
                request_target=request.url.raw_path,
            )
            raise
        except ExchangeFailed as cause:
            raise self._remember_failure(
                ApiFailure(operation, str(cause), cause=cause),
                issuance=issuance,
                attempt_mutation=attempt_mutation,
                request_method=method,
                request_target=request.url.raw_path,
            ) from None
        try:
            _require_api_envelope(response, operation, self._expected_topology)
        except ApiFailure as failure:
            self._remember_failure(
                failure,
                issuance=issuance,
                attempt_mutation=attempt_mutation,
                request_method=method,
                request_target=request.url.raw_path,
            )
            raise
        return response
