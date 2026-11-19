"""Own signed API operations; scientific admission remains with the consumer.

Wire authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/routes.py::JOB_INPUT_READ_ROUTE and
nmr_api/security/analysis_kind_refs.py. The response ceiling is the API's
131072-byte route contract, not an estimate from a typical input envelope.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import secrets
import time

import httpcore

from provider_core.api_errors import ApiFailure
from provider_core.adapters.nmr.v1.response_envelope import require_api_envelope, require_api_headers

from provider_core.adapters.nmr.v1.discovery import inventory_query, inventory_route, jobs_query, jobs_route
from provider_core.validation.upstream.nmr.v1.attempt_inventory import read_attempt_inventory

from provider_core.adapters.nmr.v1.attempt_request import (
    attempt_route,
    complete_route,
    fail_route,
    progress_body,
    progress_route,
    start_route,
)

from provider_core.validation.upstream.nmr.v1.attempt_request import (
    read_complete_body,
    read_fail_body,
    require_analysis_kind,
    require_provider_ref,
    require_start_body,
)

from provider_core.validation.upstream.nmr.v1.attempt_response import (
    ATTEMPT_RESPONSE_BYTES,
    read_attempt,
    read_complete,
    read_fail,
    read_progress,
    read_start,
    require_attempt_ref,
)

from provider_core.adapters.nmr.v1.hello import hello_body, hello_route
from provider_core.adapters.nmr.v1.job_input import job_input_query, job_input_route
from provider_core.adapters.nmr.v1.upload_request import membership_route, read_grant_route
from provider_core.validation.upstream.nmr.v1.hello import HELLO_RESPONSE_BYTES, read_hello
from provider_core.validation.upstream.nmr.v1.job_feed import PAGE_RESPONSE_BYTES, read_jobs_page
from provider_core.validation.upstream.nmr.v1.job_input import MAX_RESPONSE_BYTES, FeedInputFacts, JobInputRejected, verify_job_input
from provider_core.validation.upstream.nmr.v1.job_refs import require_job_ref
from provider_core.response_json import ResponseRejected
from provider_core.adapters.nmr.v1.signing import signed_request
from provider_core.transport import ExchangeFailed
from provider_core.type_requirements import require_type
from provider_core.validation.upstream.nmr.v1.topology import edge_topology_matches, require_api_topology
from provider_core.validation.upstream.nmr.v1.response_envelope import is_api_success

from provider_core.validation.upstream.nmr.v1.recovery import (
    API_RECOVERY_STATUS,
    access_refused,
    attempt_change_refused,
    encode_request_route,
    issuance_refused,
    matches_request_route,
    read_grant_not_finalized,
    signature_refresh_allowed,
)

from provider_core.network import network_availability

from provider_core.validation.upstream.nmr.v1.upload_response import (
    GRANT_RESPONSE_BYTES,
    MEMBERSHIP_RESPONSE_BYTES,
    read_grant,
    read_membership,
    require_upload_ref,
)


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
            (self._retryable_failure(failure) or
             self._settled_status_failure(failure, API_RECOVERY_STATUS) or
             self._settled_status_failure(failure, 429)) and
            (failure.request_method == "GET")
        )

    def can_renew_read_grant(self, failure, *, job_ref, upload_ref):
        """Permit fresh issuance after this exact settled grant failure.

        A fresh capability may extend Upload retention. This is not replay or
        recovery of an earlier bearer and never retries the POST implicitly.
        """
        require_job_ref(job_ref)
        require_upload_ref(upload_ref)

        if (
            (type(failure) is not ApiFailure) or
            (failure is not self.last_failure) or
            (not self.retirement_confirmed)
        ):
            return False

        route = read_grant_route(job_ref, upload_ref)

        if not matches_request_route(failure.request_method, failure.request_target, route):
            return False

        return (
            self._retryable_failure(failure) or
            ((failure.response is not None) and
             read_grant_not_finalized(failure.status, failure.problem_code))
        )

    def can_retry_job_discovery(self, failure):
        """Permit paced discovery reads while provider access may recover.

        Only this API's settled Jobs GET qualifies. Correlated authentication
        and authorization refusals can persist; permission does not promise
        recovery. The consumer owns stop checks, pacing and selection, and no
        returned page grants scientific execution.
        """

        if (
            (type(failure) is not ApiFailure) or
            (failure is not self.last_failure) or
            (not self.retirement_confirmed)
        ):
            return False

        route = jobs_route()

        if not matches_request_route(
            failure.request_method,
            failure.request_target,
            route,
            allow_query=True,
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

        if not (
            self._retryable_failure(failure) or
            self._settled_status_failure(failure, API_RECOVERY_STATUS) or
            self._settled_status_failure(failure, 429)
        ):
            return False

        route = hello_route()

        return matches_request_route(failure.request_method, failure.request_target, route)

    def can_refresh_signature(self, failure):
        """Admit a settled no-effect request refusal for existing retry owners.

        This alone grants no operation replay. The caller must use its existing
        route/body permission, request deadlines and retry/stop policy, and retain
        the refusal. Other authentication failures do not qualify for signature
        refresh through this predicate. The
        complete correlated problem was admitted by http_failure; an incomplete
        response or unsettled exchange cannot authorize this decision.

        Authority: nmr-api-v1 e7790cf6a922c39261fe736d5a23d17a9a5ea322,
        nmr_api/security/problems.py:826-841 (ReplayAdmissionExpired and
        signature_expired): the operation did not run; retry with a new signature.
        Pinned OpenAPI's request_body_timeout408 has the same fresh-signing rule;
        security/problems.py::RequestBodyTimeout establishes no domain change.
        This describes this send, never an earlier unacknowledged request.
        """

        return (
            (type(failure) is ApiFailure) and
            (failure is self.last_failure) and
            self.retirement_confirmed and
            (failure.response is not None) and
            signature_refresh_allowed(failure.status, failure.problem_code)
        )

    def can_replay_start(self, failure):
        """Permit unchanged durable START replay after settled send uncertainty.

        The starter must retain the original Job, provider key and body, and
        admit the returned current state before execution. This predicate grants
        neither a new key nor a scientific rerun. Authority: pinned OpenAPI's
        /x-nmr-attempt-recovery/start, START x-nmr-recovery and HTTP500 response;
        source 12a53e720fc87e1a4144c76203f8853d5afaf753 (fixture manifest).
        """

        if not self._recoverable_attempt_failure(failure):
            return False

        route = start_route()

        return matches_request_route(failure.request_method, failure.request_target, route)

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

        if not self._recoverable_attempt_failure(failure):
            return False

        route = progress_route(execution_attempt_ref)

        return matches_request_route(failure.request_method, failure.request_target, route)

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

        if (type(failure) is not ApiFailure) or (failure is not self.last_failure) or (not self.retirement_confirmed):
            return False

        completion = complete_route()
        failure_publication = fail_route()

        if (failure.request_method, failure.request_target) not in (
            encode_request_route(completion),
            encode_request_route(failure_publication),
        ):
            return False

        if self._recoverable_attempt_failure(failure):
            return True

        return self._settled_status_failure(failure, 429) or self._access_refusal(failure)

    def _settled_status_failure(self, failure, status):
        """Recognize one settled origin/edge status, not operation permission."""

        if (type(failure) is not ApiFailure) or (failure is not self.last_failure) or (not self.retirement_confirmed):
            return False

        head = failure._received_head()

        if (head is not None) and (head.status == status) and (self._gateway_environment_matches(head)):
            # Error/rate replies may come from an edge with a non-API body.
            # An interrupted body still needs settled, clean transport evidence.
            exchange = failure.cause

            return (
                (failure.response is not None) or
                ((type(exchange) is ExchangeFailed) and
                 (not exchange.cleanup_failures) and
                 (not isinstance(exchange.interruption, asyncio.CancelledError)) and
                 (exchange.caller_interruption is None) and
                 network_availability(exchange.primary, exchange.network_io, network_failure=exchange.network_failure))
            )

        return False

    def _access_refusal(self, failure):
        """Classify complete correlated refusals after the caller admits ownership."""

        # These refusals can persist. Each operation owns whether another
        # request is safe; this evidence does not establish absence of effects.
        return (
            (failure.response is not None) and
            access_refused(failure.status, failure.problem_code)
        )

    def _recoverable_attempt_failure(self, failure):
        """Share settled send evidence; public methods own exact route permission."""

        if (type(failure) is not ApiFailure) or (failure is not self.last_failure) or (not self.retirement_confirmed):
            return False

        if self._retryable_failure(failure) or self._settled_status_failure(failure, API_RECOVERY_STATUS):
            return True

        # Pinned Attempt recovery admits HTTP500 under its exact retained
        # request and operation-specific replay rules.
        if failure.response is not None:
            return False

        exchange = failure.cause

        if (
            (type(exchange) is not ExchangeFailed) or
            exchange.cleanup_failures or
            isinstance(exchange.interruption, asyncio.CancelledError) or
            (exchange.caller_interruption is not None)
        ):
            return False

        return exchange.response_head is None and type(exchange.primary) is httpcore.RemoteProtocolError

    def _retryable_failure(self, failure):
        """Share settled failure evidence, not operation retry authority."""

        if (type(failure) is not ApiFailure) or (failure is not self.last_failure) or (not self.retirement_confirmed):
            return False

        if failure.response is not None:
            return self.can_refresh_signature(failure) or self._retryable_gateway(failure.response)

        exchange = failure.cause

        if (
            (type(exchange) is not ExchangeFailed) or
            exchange.cleanup_failures or
            isinstance(exchange.interruption, asyncio.CancelledError) or
            (exchange.caller_interruption is not None)
        ):
            return False

        if exchange.response_head is not None:
            if exchange.response_head.status in (502, 503, 504):
                return self._retryable_gateway(exchange.response_head) and network_availability(exchange.primary, exchange.network_io, network_failure=exchange.network_failure)

            try:
                require_api_headers(exchange.response_head, failure.operation, self._expected_topology)
            # silent-defaults: allow-exception-outcome -- Unadmitted response headers grant no read retry
            except ApiFailure:
                return False

            if not is_api_success(exchange.response_head.status):
                return False

        if type(exchange.primary) is httpcore.ProxyError:
            # CONNECT gateway failures are availability evidence, not origin
            # responses or proof their underlying DNS/dial cause is temporary.
            # Missing status and policy refusal grant no retry permission;
            # successful CONNECT cannot authorize retry of a later TLS failure.
            return exchange.proxy_connect_status in (502, 503, 504)

        return network_availability(exchange.primary, exchange.network_io, network_failure=exchange.network_failure)

    def _retryable_gateway(self, response):
        return response.status in (502, 503, 504) and self._gateway_environment_matches(response)

    def _gateway_environment_matches(self, response):
        return edge_topology_matches(response.headers, self._expected_topology)

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
        request_method, request_path = hello_route()
        operation = "confirm provider Hello acceptance"

        try:
            response = await self._exchange(
                operation,
                method=request_method,
                path=request_path,
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
            # This effect differs from Attempt replay and grant issuance.
            # Keep their flags unchanged while preserving Hello's uncertainty safely.
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

        request_method, request_path = jobs_route()

        response = await self._exchange(
            "list Jobs",
            owner=owner,
            method=request_method,
            path=request_path,
            query=query,
            maximum_bytes=PAGE_RESPONSE_BYTES
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
        request_method, request_path = inventory_route()

        response = await self._exchange(
            "list in-progress Attempts",
            owner=owner,
            method=request_method,
            path=request_path,
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

        require_type(feed, FeedInputFacts)

        require_analysis_kind(analysis_kind_ref)

        request_method, request_path = job_input_route(feed.job_ref)

        response = await self._exchange(
            "read Job input",
            owner=owner,
            method=request_method,
            path=request_path,
            query=job_input_query(analysis_kind_ref),
            maximum_bytes=MAX_RESPONSE_BYTES,
        )

        try:
            return verify_job_input(response.body, feed=feed)
        except JobInputRejected as cause:
            raise self._remember_failure(ApiFailure("read Job input", str(cause), response=response, cause=cause)) from None

    async def uploads(self, job_ref):
        """Observe current membership once; no historical or cached authority."""
        require_job_ref(job_ref)
        request_method, request_path = membership_route(job_ref)

        response = await self._exchange(
            "read Job Upload membership",
            method=request_method,
            path=request_path,
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
        request_method, request_path = read_grant_route(job_ref, upload_ref)

        response = await self._exchange(
            "issue Upload read grant",
            method=request_method,
            path=request_path,
            query=b"",
            maximum_bytes=GRANT_RESPONSE_BYTES,
            issuance=True,
        )

        try:
            return read_grant(response.body, job_ref=job_ref, upload_ref=upload_ref)
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("issue Upload read grant", str(cause), response=response, cause=cause),
                issuance=True
            ) from None

    async def read_attempt(self, execution_attempt_ref, *, job_ref, owner=None):
        """Observe both states for the locally owned Attempt and Job.

        The expected Job binds readback to the caller's retained obligation.
        Neither state proves that scientific work ran or that particular result
        bytes committed; recovery must retain its own execution/publication facts.
        """
        require_attempt_ref(execution_attempt_ref)
        require_job_ref(job_ref)
        request_method, request_path = attempt_route(execution_attempt_ref)

        response = await self._exchange(
            "read Execution Attempt",
            owner=owner,
            method=request_method,
            path=request_path,
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
        request_method, request_path = start_route()

        response = await self._exchange(
            "start Execution Attempt",
            method=request_method,
            path=request_path,
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
                ApiFailure("start Execution Attempt", str(cause), response=response, cause=cause),
                attempt_mutation=True
            ) from None

    async def progress_attempt(self, execution_attempt_ref, *, phase, condition_code, owner=None):
        """Publish current facts once; never replay obsolete progress automatically.

        The lifecycle owner decides whether these facts still describe its latest
        observation. Neither preparing nor running proves model work or liveness.
        Public condition codes must come from the consumer's reviewed vocabulary.
        """
        require_attempt_ref(execution_attempt_ref)
        body = progress_body(phase, condition_code)
        request_method, request_path = progress_route(execution_attempt_ref)

        response = await self._exchange(
            "report Attempt progress",
            owner=owner,
            method=request_method,
            path=request_path,
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )

        try:
            return read_progress(
                response.body,
                execution_attempt_ref=execution_attempt_ref,
                phase=phase,
                condition_code=condition_code
            )
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("report Attempt progress", str(cause), response=response, cause=cause),
                attempt_mutation=True
            ) from None

    async def complete_attempt(self, body, *, execution_attempt_ref, owner=None):
        """Publish or reconcile exact retained result bytes without rerunning work.

        The caller retains this terminal choice and exact body for any permitted replay.
        Uncertainty never authorizes replacing it with another result or failure.
        """
        require_attempt_ref(execution_attempt_ref)
        (result_schema_id, submitted_result) = read_complete_body(body, execution_attempt_ref=execution_attempt_ref)
        request_method, request_path = complete_route()

        response = await self._exchange(
            "complete Execution Attempt",
            method=request_method,
            path=request_path,
            owner=owner,
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )

        try:
            return read_complete(
                response.body,
                execution_attempt_ref=execution_attempt_ref,
                result_schema_id=result_schema_id,
                result=submitted_result
            )
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("complete Execution Attempt", str(cause), response=response, cause=cause),
                attempt_mutation=True
            ) from None

    async def fail_attempt(self, body, *, execution_attempt_ref, owner=None):
        """Publish or reconcile the retained public failure, never a replacement.

        Composition owns publishable text and private evidence retention. Core
        preserves exact accepted text; it does not guess which content is secret.
        """
        require_attempt_ref(execution_attempt_ref)
        (failure_code, failure_message) = read_fail_body(body, execution_attempt_ref=execution_attempt_ref)
        request_method, request_path = fail_route()

        response = await self._exchange(
            "publish Attempt failure",
            method=request_method,
            path=request_path,
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
                ApiFailure("publish Attempt failure", str(cause), response=response, cause=cause),
                attempt_mutation=True
            ) from None

    # silent-defaults: allow-contract -- Only the exchange owner supplies a known signed method/target for operation-specific recovery; parser failures grant no replay authority.
    def _remember_failure(self, failure, *, issuance=False, attempt_mutation=False, request_method=None, request_target=None):
        failure.request_method = request_method
        failure.request_target = request_target

        if (issuance) and (not issuance_refused(failure.status, failure.problem_code)):
            failure.issuance_may_have_committed = True

            failure.add_note(
                "Download access may already have been issued and extended Upload retention; no retry was attempted."
            )

        if (attempt_mutation) and (not attempt_change_refused(failure.status, failure.problem_code)):
            failure.attempt_mutation_may_have_committed = True

            failure.add_note(
                "The Attempt change may already have committed; retain the original request for reconciliation. No retry was attempted."
            )

        self.last_failure = failure

        return failure

    # silent-defaults: allow-contract -- Callers explicitly mark issuance and Attempt effects; ordinary exchanges acquire neither effect claim.
    async def _exchange(
        self,
        operation,
        *,
        method,
        path,
        query,
        maximum_bytes,
        issuance=False,
        body=None,
        attempt_mutation=False,
        owner=None
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
            require_api_envelope(response, operation, self._expected_topology)
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
