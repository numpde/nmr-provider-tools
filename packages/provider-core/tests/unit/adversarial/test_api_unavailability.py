"""Operation-specific retry permission does not claim transience or a write outcome.

Problem fixture: API6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
security/problems.py::service_unavailable_problem_spec and problem_kinds.py.
Hello refresh and exact START/terminal replay have separate permission; no
scientific execution or other mutation acquires retry authority here.
"""

import asyncio
import errno
import json
import socket
import ssl
import unittest
from builtins import ExceptionGroup
from functools import partial
from unittest.mock import patch

import httpcore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.attempt_request import complete_body, fail_body
from provider_core.hello import AnalysisOffering
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed, HttpResponseHead
from tests.fakes.api import Transport, reply, response_headers, signature_window_problem
from tests.fakes.attempts import ATTEMPT_REF, retained_start
from tests.fakes.job_input import facts
from tests.fakes.uploads import UPLOAD_REF


def unavailable_problem(**changes):
    document = {
        "type": "urn:nmr-api:problem:service-unavailable",
        "title": "Service unavailable",
        "status": 503,
        "instance": "urn:nmr-api:request:test",
        "request_id": "request:test",
        "code": "service_unavailable",
        "detail": "PRIVATE_SERVER_DETAIL",
    }
    return json.dumps(document | changes).encode()


def body_timeout_problem(**changes):
    return unavailable_problem(**({"type": "urn:nmr-api:problem:request-body-timeout", "title": "Request body timeout",
                                  "status": 408, "code": "request_body_timeout"} | changes))


class ApiAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def failed_discovery(self, response):
        transport = Transport(response)
        api = ProviderAPI(SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
                          transport, expected_topology="dev-local")
        with self.assertRaises(ApiFailure) as caught:
            await api.jobs(analysis_kind_ref="nmr_predict", has_provider_execution_attempt=False, limit=1, cursor=None)
        return api, transport, caught.exception

    async def test_discovery_can_pace_access_refusals_without_granting_mutation_permissions(self):
        responses = [reply(signature_window_problem(code=code), status=401) for code in (
            "authentication_failed", "authentication_nonce_reused", "signature_created_in_future",
        )]
        responses += [reply(unavailable_problem(type="urn:nmr-api:problem:authorization-denied", status=403,
                                                code="authorization_denied"), status=403),
                      reply(b"rate limited", status=429, headers=((b"retry-after", b"10"),))]
        for response in responses:
            with self.subTest(status=response.status):
                api, transport, failure = await self.failed_discovery(response)
                self.assertTrue(api.can_retry_job_discovery(failure))
                self.assertIs(api.can_retry_read(failure), response.status == 429)
                self.assertFalse(api.can_refresh_hello(failure))
                self.assertFalse(api.can_replay_terminal(failure))
                self.assertEqual(len(transport.calls), 1)
                transport.retirement_confirmed = False
                self.assertFalse(api.can_retry_job_discovery(failure))
                await api.retire()
                self.assertTrue(api.can_retry_job_discovery(failure))
                self.assertFalse(api.can_retry_job_discovery(ApiFailure("foreign read", "unknown")))
                self.assertNotIn("PRIVATE_SERVER_DETAIL", str(failure))

    async def test_discovery_permission_refuses_untrusted_refusals_and_unrelated_routes(self):
        for response in (
            reply(b"rate limited", status=429, headers=response_headers(status=429, topology=b"web")),
            reply(signature_window_problem(code="authentication_failed"), status=401,
                  headers=response_headers(status=401, request_id=b"request:other")),
            reply(signature_window_problem(code="unreviewed_code"), status=401),
            reply(b"denied", status=403), reply(b"not found", status=404), reply(b"bad request", status=400),
            reply(b"malformed page"),
            ExchangeFailed("Cleanup failed", primary=httpcore.ReadTimeout(), cleanup_failures=(OSError(),)),
            ExchangeFailed("Cancelled", primary=httpcore.ReadTimeout(), interruption=asyncio.CancelledError()),
        ):
            with self.subTest(response=type(response).__name__):
                api, transport, failure = await self.failed_discovery(response)
                self.assertFalse(api.can_retry_job_discovery(failure))
                self.assertEqual(len(transport.calls), 1)
        api, transport, failure = await self.failed_read(reply(b"maintenance", status=503))
        self.assertFalse(api.can_retry_job_discovery(failure))
        for method, path in (("GET", "/provider/v1/jobs/other"), ("POST", "/provider/v1/jobs")):
            transport.replies = iter((reply(b"maintenance", status=503),))
            with self.assertRaises(ApiFailure) as caught:
                await api._exchange("list Jobs", method=method, path=path, query=b"", maximum_bytes=4096)
            self.assertFalse(api.can_retry_job_discovery(caught.exception))

    async def test_safe_read_and_latest_hello_use_settled_internal_error_or_verified_body_timeout(self):
        for response, expected in (
            (reply(b"rate limited", status=429, headers=((b"retry-after", b"10"),)), True),
            (reply(b"rate limited", status=429, headers=response_headers(status=429, topology=b"web")), False),
            (reply(b"internal error", status=500), True),
            (reply(b"internal error", status=500, headers=response_headers(status=500, topology=b"web")), False),
            (reply(body_timeout_problem(), status=408), True),
            (reply(body_timeout_problem(request_id="request:other"), status=408), False),
            (reply(body_timeout_problem(code="unreviewed_code"), status=408), False),
            (reply(body_timeout_problem(), status=408, headers=response_headers(status=408, topology=b"web")), False),
            (reply(b"timeout", status=408), False),
            (reply(signature_window_problem(), status=401), True),
            (ExchangeFailed("Interrupted internal error", primary=httpcore.ReadTimeout(),
                            response_head=HttpResponseHead(500, response_headers(status=500))), True),
            (ExchangeFailed("Unclean internal error", primary=httpcore.ReadTimeout(),
                            response_head=HttpResponseHead(500, response_headers(status=500)), cleanup_failures=(OSError(),)), False),
            (ExchangeFailed("Cancelled internal error", primary=httpcore.ReadTimeout(),
                            response_head=HttpResponseHead(500, response_headers(status=500)), interruption=asyncio.CancelledError()), False),
            (ExchangeFailed("Interrupted rate limit", primary=httpcore.ReadTimeout(),
                            response_head=HttpResponseHead(429, response_headers(status=429))), True),
            (ExchangeFailed("Interrupted timeout refusal", primary=httpcore.ReadTimeout(),
                            response_head=HttpResponseHead(408, response_headers(status=408))), False),
        ):
            with self.subTest(response=type(response).__name__, expected=expected):
                api, transport, failure = await self.failed_read(response)
                self.assertIs(api.can_retry_read(failure), expected)
                self.assertNotIn("PRIVATE_SERVER_DETAIL", str(failure))
                transport.retirement_confirmed = False
                self.assertFalse(api.can_retry_read(failure))
                await api.retire()
                self.assertIs(api.can_retry_read(failure), expected)
                transport.replies = iter((response,))
                with self.assertRaises(ApiFailure) as caught:
                    await api.publish_hello(provider_ref="provider:test", display_name="Test", description="Test",
                                            analysis_offerings=(AnalysisOffering("peaks_from_mol", "Test"),))
                self.assertIs(api.can_refresh_hello(caught.exception), expected)
                self.assertFalse(api.can_retry_read(caught.exception))
                transport.retirement_confirmed = False
                self.assertFalse(api.can_refresh_hello(caught.exception))
                await api.retire()
                self.assertIs(api.can_refresh_hello(caught.exception), expected)
                self.assertEqual(len(transport.calls), 2)

    async def test_verified_body_timeout_preserves_each_exact_operation_permission(self):
        for operation in ("start", "complete", "fail", "progress", "condition"):
            with self.subTest(operation=operation):
                api, _, failure = await self.failed_replay_operation(reply(body_timeout_problem(), status=408), operation=operation)
                self.assertEqual(failure.problem_code, "request_body_timeout")
                self.assertTrue(api.can_refresh_signature(failure))
                self.assertIs(api.can_replay_start(failure), operation == "start")
                self.assertIs(api.can_replay_terminal(failure), operation in ("complete", "fail"))
                self.assertIs(api.can_retry_condition(failure, execution_attempt_ref=ATTEMPT_REF), operation in ("progress", "condition"))
                self.assertIs(api.can_defer_phase_progress(failure, execution_attempt_ref=ATTEMPT_REF), operation in ("progress", "condition"))
                self.assertFalse(failure.attempt_mutation_may_have_committed)

    async def test_terminal_refusals_retry_without_granting_other_operation_permissions(self):
        denied = unavailable_problem(type="urn:nmr-api:problem:authorization-denied", status=403,
                                     code="authorization_denied")
        responses = [reply(signature_window_problem(code=code), status=401) for code in (
            "authentication_failed", "authentication_nonce_reused", "signature_created_in_future",
        )]
        responses += [reply(denied, status=403), reply(b"rate limited", status=429,
                      headers=((b"content-type", b"text/html"),))]
        for operation in ("start", "complete", "fail", "progress", "condition"):
            for response in responses:
                with self.subTest(operation=operation, status=response.status):
                    api, transport, failure = await self.failed_replay_operation(response, operation=operation)
                    self.assertIs(api.can_replay_terminal(failure), operation in ("complete", "fail"))
                    self.assertFalse(api.can_replay_start(failure))
                    self.assertFalse(api.can_retry_condition(failure, execution_attempt_ref=ATTEMPT_REF))
                    self.assertFalse(api.can_retry_read(failure))
                    self.assertFalse(api.can_refresh_hello(failure))
                    self.assertEqual(failure.status, response.status)
                    self.assertNotIn("PRIVATE_SERVER_DETAIL", str(failure))
                    transport.retirement_confirmed = False
                    self.assertFalse(api.can_replay_terminal(failure))

    async def test_terminal_retry_still_refuses_untrusted_or_unknown_authentication_evidence(self):
        for response in (
            reply(b"rate limited", status=429, headers=response_headers(status=429, topology=b"web")),
            reply(b"rate limited", status=429, headers=((b"nmr-api-topology", b"dev-local"),
                                                       (b"nmr-api-topology", b"dev-local"))),
            reply(signature_window_problem(code="authentication_failed"), status=401,
                  headers=response_headers(status=401, request_id=b"request:other")),
            reply(signature_window_problem(code="unreviewed_code"), status=401),
            reply(b"denied", status=403),
        ):
            with self.subTest(status=response.status, headers=response.headers):
                api, _, failure = await self.failed_replay_operation(response, operation="complete")
                self.assertFalse(api.can_replay_terminal(failure))

    def test_retry_after_accepts_http_time_forms_without_changing_retry_authority(self):
        with patch("provider_core.api.time.time", return_value=784111777):
            for value, expected in (
                (b"120", 120), (b"0", 0), (b"Sun, 06 Nov 1994 08:49:37 GMT", 0),
                (b"Sun, 06 Nov 1994 08:51:37 GMT", 120), (b"Sunday, 06-Nov-94 08:51:37 GMT", 120),
                (b"Sun Nov  6 08:51:37 1994", 120), (b"-3", None), (b"0.1", None),
                (b"not a date", None), (b"9" * 400, None), (b"\xff", None),
            ):
                with self.subTest(value=value):
                    failure = ApiFailure("publish result", "refused", response=reply(
                        b"rate limited", status=429, headers=((b"retry-after", value),)
                    ))
                    self.assertEqual(failure.retry_after_seconds, expected)
            duplicate = ApiFailure("publish result", "refused", response=reply(
                b"rate limited", status=429, headers=((b"retry-after", b"120"), (b"retry-after", b"240"))
            ))
            self.assertIsNone(duplicate.retry_after_seconds)

    async def test_connect_gateway_status_requires_the_owned_proxy_failure_and_settled_io(self):
        for status, expected in ((None, False), (200, False), (403, False), (502, True), (503, True), (504, True)):
            with self.subTest(status=status):
                exchange = ExchangeFailed(
                    "CONNECT failed", primary=httpcore.ProxyError("PRIVATE_PROXY_REASON"), proxy_connect_status=status
                )
                api, transport, failure = await self.failed_read(exchange)
                self.assertIs(api.can_retry_read(failure), expected)
                transport.retirement_confirmed = False
                self.assertFalse(api.can_retry_read(failure))
                await api.retire()
                exchange.cleanup_failures = (OSError(errno.EIO, "PRIVATE_CLEANUP"),)
                self.assertFalse(api.can_retry_read(failure))
                exchange.cleanup_failures = ()
                exchange.interruption = asyncio.CancelledError()
                self.assertFalse(api.can_retry_read(failure))
        exchange = ExchangeFailed(
            "TLS failed after CONNECT", primary=httpcore.ConnectError("certificate invalid"), proxy_connect_status=200
        )
        api, _, failure = await self.failed_read(exchange)
        self.assertFalse(api.can_retry_read(failure))

    async def test_signature_refresh_requires_a_complete_owned_retired_read_refusal(self):
        for changes, headers, expected in (
            ({}, response_headers(status=401, request_id=b"request:test"), True),
            ({"code": "signature_expired"}, response_headers(status=401, request_id=b"request:test"), True),
            ({"code": "signature_created_in_future"}, response_headers(status=401, request_id=b"request:test"), False),
            ({"code": "authentication_failed"}, response_headers(status=401, request_id=b"request:test"), False),
            ({"code": "authentication_nonce_reused"}, response_headers(status=401, request_id=b"request:test"), False),
            ({"request_id": "request:other"}, response_headers(status=401, request_id=b"request:test"), False),
            ({}, response_headers(status=401, request_id=b"request:test", topology=b"web"), False),
            ({}, response_headers(status=401), False),
        ):
            body = json.dumps(json.loads(signature_window_problem()) | changes).encode()
            with self.subTest(changes=changes, headers=headers):
                api, transport, failure = await self.failed_read(reply(body, status=401, headers=headers))
                self.assertIs(api.can_refresh_signature(failure), expected)
                self.assertIs(api.can_retry_read(failure), expected)
                transport.retirement_confirmed = False
                self.assertFalse(api.can_refresh_signature(failure))
                await api.retire()
                self.assertIs(api.can_refresh_signature(failure), expected)
                self.assertFalse(api.can_refresh_signature(ApiFailure("foreign read", "unknown")))
                self.assertNotIn("PRIVATE_SERVER_DETAIL", str(failure))

    async def test_signature_refresh_keeps_each_existing_attempt_operation_permission(self):
        for code in ("authentication_window_closed", "signature_expired"):
            for operation in ("start", "complete", "fail", "progress", "condition"):
                with self.subTest(code=code, operation=operation):
                    api, transport, failure = await self.failed_replay_operation(
                        reply(signature_window_problem(code=code), status=401), operation=operation
                    )
                    self.assertTrue(api.can_refresh_signature(failure))
                    self.assertFalse(api.can_retry_read(failure))
                    self.assertFalse(api.can_refresh_hello(failure))
                    self.assertIs(api.can_replay_start(failure), operation == "start")
                    self.assertIs(api.can_replay_terminal(failure), operation in ("complete", "fail"))
                    self.assertIs(
                        api.can_retry_condition(failure, execution_attempt_ref=ATTEMPT_REF),
                        operation in ("progress", "condition"),
                    )
                    self.assertEqual(len(transport.calls), 2)

    async def failed_replay_operation(self, response, *, operation):
        api, transport, _ = await self.failed_read(reply(b"maintenance", status=503))
        transport.replies = iter((response,))
        with self.assertRaises(ApiFailure) as caught:
            if operation == "start":
                await api.start_attempt(
                    retained_start(), job_ref="job:example", analysis_kind_ref="nmr_predict", provider_ref="provider:example"
                )
            elif operation == "complete":
                await api.complete_attempt(
                    complete_body(ATTEMPT_REF, "fixture.result.v1", b"{}"), execution_attempt_ref=ATTEMPT_REF
                )
            elif operation in ("progress", "condition"):
                # silent-defaults: allow-contract -- Only condition replay supplies the waiting_capacity condition code.
                condition_code = "waiting_capacity" if operation == "condition" else None
                await api.progress_attempt(
                    ATTEMPT_REF, phase="preparing", condition_code=condition_code
                )
            else:
                await api.fail_attempt(
                    fail_body(ATTEMPT_REF, "fixture_failure", "Fixture stopped"), execution_attempt_ref=ATTEMPT_REF
                )
        return api, transport, caught.exception

    async def test_attempt_operation_permission_distinguishes_unverified_response_from_received_refusal(self):
        for operation in ("start", "complete", "fail", "progress", "condition"):
            for response, expected in (
                (reply(b"maintenance", status=503), True),
                (reply(b"internal error", status=500), True),
                (reply(b"internal error", status=500, headers=response_headers(status=500, topology=b"web")), False),
                (
                    ExchangeFailed(
                        "Interrupted error body",
                        primary=httpcore.ReadTimeout(),
                        response_head=HttpResponseHead(500, response_headers(status=500)),
                    ),
                    True,
                ),
                (ExchangeFailed("No verified head", primary=httpcore.RemoteProtocolError("PRIVATE_WIRE")), True),
                (ExchangeFailed("Read timed out", primary=httpcore.ReadTimeout()), True),
                (reply(b"malformed receipt"), False),
                (reply(b"denied", status=401), False),
                (reply(b"conflict", status=409), False),
                (reply(b"maintenance", status=503, headers=response_headers(status=503, topology=b"web")), False),
                (
                    ExchangeFailed(
                        "Malformed body",
                        primary=httpcore.RemoteProtocolError(),
                        response_head=HttpResponseHead(200, response_headers()),
                    ),
                    False,
                ),
                (ExchangeFailed("Cleanup failed", primary=httpcore.RemoteProtocolError(), cleanup_failures=(OSError(),)), False),
                (
                    ExchangeFailed("Cancelled", primary=httpcore.RemoteProtocolError(), interruption=asyncio.CancelledError()),
                    False,
                ),
                (ExchangeFailed("Certificate rejected", primary=ssl.SSLCertVerificationError()), False),
            ):
                with self.subTest(operation=operation, response=response):
                    api, transport, failure = await self.failed_replay_operation(response, operation=operation)
                    permission = {
                        "start": api.can_replay_start,
                        "complete": api.can_replay_terminal,
                        "fail": api.can_replay_terminal,
                        "progress": partial(api.can_defer_phase_progress, execution_attempt_ref=ATTEMPT_REF),
                        "condition": partial(api.can_retry_condition, execution_attempt_ref=ATTEMPT_REF),
                    }[operation]
                    other_replay = api.can_replay_terminal if operation == "start" else api.can_replay_start
                    self.assertIs(permission(failure), expected)
                    self.assertFalse(other_replay(failure))
                    self.assertFalse(api.can_retry_read(failure))
                    self.assertFalse(api.can_refresh_hello(failure))
                    transport.retirement_confirmed = False
                    self.assertFalse(permission(failure))
                    await api.retire()
                    self.assertIs(permission(failure), expected)
                    self.assertEqual(len(transport.calls), 2)

    async def test_progress_deferral_requires_this_attempts_exact_current_retired_put(self):
        api, transport, original = await self.failed_replay_operation(reply(b"maintenance", status=503), operation="progress")
        self.assertTrue(api.can_defer_phase_progress(original, execution_attempt_ref=ATTEMPT_REF))
        self.assertFalse(api.can_replay_start(original))
        self.assertFalse(api.can_replay_terminal(original))
        self.assertFalse(api.can_defer_phase_progress(original, execution_attempt_ref="execution_attempt:sha256:" + "e" * 64))
        target = "/provider/v1/execution-attempts/" + ATTEMPT_REF + "/progress"
        operations = (
            ("GET", target, b""),
            ("PUT", target + "/", b""),
            ("PUT", target, b"extra=1"),
            ("PUT", "/provider/v1/hello", b""),
        )
        transport.replies = iter([reply(b"maintenance", status=503)] * len(operations))
        for method, path, query in operations:
            with self.subTest(method=method, path=path, query=query), self.assertRaises(ApiFailure) as caught:
                await api._exchange("untrusted label", method=method, path=path, query=query, maximum_bytes=4096, body=b"{}")
            self.assertFalse(api.can_defer_phase_progress(caught.exception, execution_attempt_ref=ATTEMPT_REF))
            self.assertFalse(api.can_defer_phase_progress(original, execution_attempt_ref=ATTEMPT_REF))
        self.assertFalse(api.can_defer_phase_progress(ApiFailure("progress", "foreign owner"), execution_attempt_ref=ATTEMPT_REF))
        self.assertEqual(len(transport.calls), 2 + len(operations))

    async def test_terminal_replay_requires_this_apis_current_failure_and_exact_unqueried_terminal_target(self):
        api, transport, old = await self.failed_replay_operation(reply(b"maintenance", status=503), operation="complete")
        operations = (
            ("GET", "/provider/v1/execution-attempts/complete", b""),
            ("POST", "/provider/v1/execution-attempts/start", b""),
            ("POST", "/provider/v1/hello", b""),
            ("POST", "/provider/v1/execution-attempts/complete", b"extra=1"),
            ("POST", "/provider/v1/execution-attempts/fail/", b""),
        )
        transport.replies = iter([reply(b"maintenance", status=503)] * len(operations))
        for method, path, query in operations:
            with self.subTest(method=method, path=path, query=query), self.assertRaises(ApiFailure) as caught:
                await api._exchange("untrusted label", method=method, path=path, query=query, maximum_bytes=4096, body=b"{}")
            self.assertFalse(api.can_replay_terminal(caught.exception))
            self.assertFalse(api.can_replay_terminal(old))

    async def test_start_replay_requires_current_owned_failure_and_exact_unqueried_start_target(self):
        api, transport, old = await self.failed_replay_operation(reply(b"maintenance", status=503), operation="start")
        self.assertTrue(api.can_replay_start(old))
        operations = (
            ("GET", "/provider/v1/execution-attempts/start", b""),
            ("POST", "/provider/v1/execution-attempts/start/", b""),
            ("POST", "/provider/v1/execution-attempts/start", b"extra=1"),
            ("POST", "/provider/v1/execution-attempts/complete", b""),
            ("POST", "/provider/v1/hello", b""),
        )
        transport.replies = iter([reply(b"maintenance", status=503)] * len(operations))
        for method, path, query in operations:
            with self.subTest(method=method, path=path, query=query), self.assertRaises(ApiFailure) as caught:
                await api._exchange(
                    "start Execution Attempt", method=method, path=path, query=query, maximum_bytes=4096, body=b"{}"
                )
            self.assertFalse(api.can_replay_start(caught.exception))
            self.assertFalse(api.can_replay_start(old))
        self.assertFalse(api.can_replay_start(ApiFailure("start Execution Attempt", "foreign owner")))
        self.assertEqual(len(transport.calls), 2 + len(operations))

    async def test_start_replay_uses_late_settled_evidence_and_refuses_unreviewed_failure_subtypes(self):
        exchange = ExchangeFailed("Head not yet observed", primary=httpcore.RemoteProtocolError())
        api, transport, failure = await self.failed_replay_operation(exchange, operation="start")
        transport.retirement_confirmed = False
        self.assertFalse(api.can_replay_start(failure))
        exchange.response_head = HttpResponseHead(500, response_headers(status=500, topology=b"web"))
        await api.retire()
        self.assertFalse(api.can_replay_start(failure))
        exchange.response_head = None
        self.assertTrue(api.can_replay_start(failure))
        exchange.cleanup_failures = (OSError(errno.EIO, "PRIVATE_CLEANUP"),)
        self.assertFalse(api.can_replay_start(failure))

        class ForeignFailure(ApiFailure):
            pass

        api.last_failure = ForeignFailure("start Execution Attempt", "not the admitted owner type")
        api.last_failure.request_method = "POST"
        api.last_failure.request_target = b"/provider/v1/execution-attempts/start"
        api.last_failure.response = reply(b"maintenance", status=503)
        self.assertFalse(api.can_replay_start(api.last_failure))
        self.assertEqual(len(transport.calls), 2)

    async def failed_read(self, response):
        transport = Transport(response)
        api = ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )
        with self.assertRaises(ApiFailure) as caught:
            await api.job_input(facts(), analysis_kind_ref="nmr_prediction")
        self.assertEqual(len(transport.calls), 1)
        return api, transport, caught.exception

    async def test_safe_read_retry_handles_proxy_outages_without_admitting_their_body(self):
        for response, expected in (
            (reply(unavailable_problem(), status=503), True),
            (reply(b"PRIVATE_HTML", status=503), True),
            (reply(b"PRIVATE_HTML", status=502, headers=((b"content-type", b"text/html"),)), True),
            (reply(b"PRIVATE_HTML", status=504, headers=()), True),
            (reply(unavailable_problem(code="unknown"), status=503), True),
            (reply(unavailable_problem(request_id="request:other"), status=503), True),
            (reply(unavailable_problem(), status=500), True),
            (reply(unavailable_problem(), status=401), False),
            (reply(unavailable_problem(), status=503, headers=response_headers(status=503, topology=b"web")), False),
        ):
            with self.subTest(status=response.status, body=response.body):
                api, _, failure = await self.failed_read(response)
                self.assertIs(api.can_retry_read(failure), expected)
                self.assertNotIn("PRIVATE", str(failure))
                if response.body != unavailable_problem() or not expected:
                    self.assertIsNone(failure.problem_code)

    async def test_reviewed_io_evidence_is_distinct_from_configuration_and_protocol_errors(self):
        refused = httpcore.ConnectError("PRIVATE_IO")
        refused.__cause__ = OSError(errno.ECONNREFUSED, "PRIVATE_ADDRESS")
        certificate = httpcore.ConnectError("PRIVATE_TLS")
        certificate.__cause__ = ssl.SSLCertVerificationError("PRIVATE_CERTIFICATE")
        wrapped_group = httpcore.ConnectError("PRIVATE_GROUP")
        wrapped_group.__cause__ = OSError("All connection attempts failed")
        wrapped_group.__cause__.__cause__ = ExceptionGroup("connect", [ConnectionRefusedError(errno.ECONNREFUSED, "private")])
        for cause, expected in (
            (TimeoutError(), True),
            (httpcore.ConnectTimeout(), True),
            (httpcore.ReadTimeout(), True),
            (refused, True),
            (wrapped_group, True),
            (socket.gaierror(socket.EAI_AGAIN, "PRIVATE_DNS"), True),
            (socket.gaierror(socket.EAI_NONAME, "PRIVATE_DNS"), False),
            (certificate, False),
            (FileNotFoundError(errno.ENOENT, "PRIVATE_SOCKET"), False),
            (PermissionError(errno.EACCES, "PRIVATE_SOCKET"), False),
            (httpcore.ConnectError("unknown"), False),
            (httpcore.RemoteProtocolError("PRIVATE_FRAMING"), False),
            (ExchangeFailed("body limit"), False),
        ):
            with self.subTest(cause=type(cause).__name__):
                api, _, failure = await self.failed_read(ExchangeFailed("Exchange failed", primary=cause))
                self.assertIs(api.can_retry_read(failure), expected)

    async def test_retirement_and_late_original_evidence_control_the_decision(self):
        exchange = ExchangeFailed("Exchange failed", primary=TimeoutError())
        api, transport, failure = await self.failed_read(exchange)
        transport.retirement_confirmed = False
        self.assertFalse(api.can_retry_read(failure))
        await api.retire()
        self.assertTrue(api.can_retry_read(failure))
        exchange.primary = ssl.SSLCertVerificationError("PRIVATE_CERTIFICATE")
        self.assertFalse(api.can_retry_read(failure))
        self.assertFalse(api.can_retry_read(ApiFailure("other API", "unknown")))

    async def test_read_retry_never_authorizes_a_hello_post(self):
        api, transport, old_read = await self.failed_read(reply(b"maintenance", status=503))
        transport.replies = iter((reply(unavailable_problem(), status=503),))
        with self.assertRaises(ApiFailure) as caught:
            await api.publish_hello(
                provider_ref="provider:test",
                display_name="Test",
                description="Test",
                analysis_offerings=(AnalysisOffering("peaks_from_mol", "Test"),),
            )
        self.assertEqual(caught.exception.request_method, "POST")
        self.assertFalse(api.can_retry_read(caught.exception))
        self.assertTrue(api.can_refresh_hello(caught.exception))
        self.assertEqual(caught.exception.request_target, b"/provider/v1/hello")
        self.assertFalse(api.can_refresh_hello(old_read))
        transport.retirement_confirmed = False
        self.assertFalse(api.can_refresh_hello(caught.exception))
        await api.retire()
        self.assertTrue(api.can_refresh_hello(caught.exception))
        self.assertEqual(len(transport.calls), 2)

    async def test_hello_refresh_permission_excludes_other_mutations_and_queried_targets(self):
        api, transport, _ = await self.failed_read(reply(b"maintenance", status=503))
        operations = (
            lambda: api.read_grant("job:example", UPLOAD_REF),
            lambda: api.start_attempt(
                retained_start(), job_ref="job:example", analysis_kind_ref="nmr_predict", provider_ref="provider:example"
            ),
            lambda: api.progress_attempt(ATTEMPT_REF, phase="running", condition_code=None),
            lambda: api.complete_attempt(
                complete_body(ATTEMPT_REF, "fixture.result.v1", b"{}"), execution_attempt_ref=ATTEMPT_REF
            ),
            lambda: api.fail_attempt(
                fail_body(ATTEMPT_REF, "fixture_failure", "Fixture stopped"), execution_attempt_ref=ATTEMPT_REF
            ),
            lambda: api._exchange(
                "same English label", method="POST", path="/provider/v1/hello", query=b"extra=1", maximum_bytes=4096, body=b"{}"
            ),
        )
        transport.replies = iter([reply(unavailable_problem(), status=503)] * len(operations))
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ApiFailure) as caught:
                await operation()
            self.assertFalse(api.can_retry_read(caught.exception))
            self.assertFalse(api.can_refresh_hello(caught.exception))
        self.assertEqual(len(transport.calls), 1 + len(operations))

    async def test_partial_response_respects_observed_environment_before_read_retry(self):
        for head, expected in (
            (HttpResponseHead(200, response_headers()), True),
            (HttpResponseHead(200, response_headers(topology=b"web")), False),
            (HttpResponseHead(503, response_headers(status=503, request_id=b"request:test")), True),
            (HttpResponseHead(503, response_headers(status=503, topology=b"web")), False),
        ):
            with self.subTest(head=head):
                api, _, failure = await self.failed_read(
                    ExchangeFailed("Exchange failed", primary=TimeoutError(), response_head=head)
                )
                self.assertIs(api.can_retry_read(failure), expected)
