"""API failures preserve private evidence, reject ambiguity and never retry."""

import asyncio
import json
import traceback
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.job_input import facts, response


class ApiReadFailureTests(unittest.IsolatedAsyncioTestCase):
    def api(self, transport):
        return ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )

    async def test_invalid_kind_never_reaches_transport(self):
        for kind in ("kind:prediction", "a&job=elsewhere", "a" * 129, None):
            transport = Transport()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                await self.api(transport).job_input(facts(), analysis_kind_ref=kind)
            self.assertEqual(transport.calls, [])

    async def test_http_failure_retains_body_without_rendering_it_or_retrying(self):
        observed = reply(b"private-response-canary", status=503)
        transport = Transport(observed)
        with self.assertRaises(ApiFailure) as caught:
            await self.api(transport).job_input(facts(), analysis_kind_ref="nmr_prediction")
        self.assertEqual(caught.exception.status, 503)
        self.assertIs(caught.exception.response, observed)
        self.assertIn("Job input", str(caught.exception))
        self.assertIn("503", str(caught.exception))
        self.assertNotIn("private-response-canary", "".join(traceback.format_exception(caught.exception)))
        self.assertEqual(len(transport.calls), 1)

    async def test_invalid_success_envelope_never_yields_input(self):
        cases = (
            (),
            ((b"content-type", b"text/html"), (b"cache-control", b"no-store")),
            ((b"content-type", b"application/json"), (b"Content-Type", b"application/json"), (b"cache-control", b"no-store")),
            ((b"content-type", b"application/json"), (b"cache-control", b"public")),
            ((b"content-type", b"application/json"), (b"cache-control", b"no-store"), (b"content-encoding", b"gzip")),
        )
        for headers in cases:
            with self.subTest(headers=headers), self.assertRaises(ApiFailure):
                await self.api(Transport(reply(response(), headers=headers))).job_input(
                    facts(), analysis_kind_ref="nmr_prediction"
                )

    async def test_verification_failure_preserves_useful_reason_and_response(self):
        observed = reply(response(job_ref="job:other"))
        with self.assertRaises(ApiFailure) as caught:
            await self.api(Transport(observed)).job_input(facts(), analysis_kind_ref="nmr_prediction")
        self.assertIn("job_ref", str(caught.exception))
        self.assertIs(caught.exception.response, observed)
        self.assertIsNotNone(caught.exception.cause)

    async def test_io_failure_and_cancellation_keep_operation_context(self):
        io_failure = ExchangeFailed("Cannot complete HTTPS exchange", primary=RuntimeError("private-io-canary"))
        for interruption in (io_failure, asyncio.CancelledError()):
            transport = Transport(interruption)
            transport.last_failure = io_failure
            api = self.api(transport)
            with (
                self.subTest(interruption=type(interruption).__name__),
                self.assertRaises(asyncio.CancelledError if isinstance(interruption, asyncio.CancelledError) else ApiFailure),
            ):
                await api.job_input(facts(), analysis_kind_ref="nmr_prediction")
            self.assertIs(api.last_failure.cause, io_failure)
            self.assertIn("Job input", str(api.last_failure))
            self.assertNotIn("private-io-canary", repr(api.last_failure))
            self.assertEqual(len(transport.calls), 1)

    async def test_problem_reason_is_local_and_unknown_or_ambiguous_problem_stays_private(self):
        document = {
            "type": "urn:nmr-api:problem:authentication-failed",
            "title": "private-title-canary",
            "status": 401,
            "instance": "urn:nmr-api:request:test",
            "request_id": "request:test",
            "code": "signature_expired",
            "detail": "private-detail-canary",
        }
        for changes, known in (
            ({}, True),
            ({"status": 404}, False),
            ({"code": "private-code-canary"}, False),
            ({"type": "urn:foreign"}, False),
            ({"request_id": "bad\nrequest"}, False),
        ):
            body = json.dumps(document | changes).encode()
            transport = Transport(reply(body, status=401, headers=response_headers(status=401, request_id=b"request:test")))
            with self.subTest(changes=changes), self.assertRaises(ApiFailure) as caught:
                await self.api(transport).job_input(facts(), analysis_kind_ref="nmr_prediction")
            failure = caught.exception
            rendered = "".join(traceback.format_exception(failure))
            self.assertNotIn("private-", rendered)
            # silent-defaults: allow-contract -- Only the known correlated problem obtains the reviewed local code.
            self.assertEqual(failure.problem_code, "signature_expired" if known else None)
            # Header correlation survives rejected/unknown body diagnostics.
            self.assertEqual(failure.request_id, "request:test")
            if known:
                self.assertIn("expired", str(failure))
                self.assertIn("clock", str(failure))
        duplicate = json.dumps(document)[:-1] + ',"code":"signature_expired"}'
        transport = Transport(reply(duplicate.encode(), status=401, headers=response_headers(status=401, request_id=b"request:test")))
        with self.assertRaises(ApiFailure) as caught:
            await self.api(transport).job_input(facts(), analysis_kind_ref="nmr_prediction")
        self.assertIsNone(caught.exception.problem_code)
