"""API failures preserve private evidence, reject ambiguity and never retry."""

import asyncio
import json
import traceback
import unittest
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.adapters.nmr.v1.signing import SigningIdentity
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
        for (kind, error_type) in (("kind:prediction", ValueError), ("a&job=elsewhere", ValueError), ("a" * 129, ValueError), (None, TypeError)):
            transport = Transport()

            with self.subTest(kind=kind), self.assertRaises(error_type):
                await self.api(transport).job_input(facts(), analysis_kind_ref=kind)

            self.assertEqual(transport.calls, [])

    async def _http_failure(self):
        observed = reply(b"private-response-canary", status=503)
        transport = Transport(observed)

        with self.assertRaises(ApiFailure) as caught:
            await self.api(transport).job_input(facts(), analysis_kind_ref="nmr_prediction")

        return caught.exception, observed, transport

    async def test_http_failure_retains_body_without_retrying(self):
        (failure, observed, transport) = await self._http_failure()

        self.assertEqual(failure.status, 503)
        self.assertIs(failure.response, observed)
        self.assertIn("Job input", str(failure))
        self.assertIn("503", str(failure))
        self.assertEqual(len(transport.calls), 1)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_http_failure_traceback_omits_response_content(self):
        (failure, _observed, _transport) = await self._http_failure()

        self.assertNotIn("private-response-canary", "".join(traceback.format_exception(failure)))

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
                await (
                    self
                    .api(Transport(reply(response(), headers=headers)))
                    .job_input(
                        facts(),
                        analysis_kind_ref="nmr_prediction",
                    )
                )

    async def test_verification_failure_preserves_useful_reason_and_response(self):
        observed = reply(response(job_ref="job:other"))

        with self.assertRaises(ApiFailure) as caught:
            await self.api(Transport(observed)).job_input(facts(), analysis_kind_ref="nmr_prediction")

        self.assertIn("job_ref", str(caught.exception))
        self.assertIs(caught.exception.response, observed)
        self.assertIsNotNone(caught.exception.cause)

    async def _interrupted_reads(self):
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

            yield api.last_failure, io_failure, transport

    async def test_io_failure_and_cancellation_keep_operation_context(self):
        async for (failure, io_failure, transport) in self._interrupted_reads():
            self.assertIs(failure.cause, io_failure)
            self.assertIn("Job input", str(failure))
            self.assertEqual(len(transport.calls), 1)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_interrupted_read_diagnostics_omit_native_text(self):
        async for (failure, _io_failure, _transport) in self._interrupted_reads():
            self.assertNotIn("private-io-canary", repr(failure))

    async def _problem_read_failures(self):
        document = {
            "type": "urn:nmr-api:problem:authentication-failed",
            "title": "private-title-canary",
            "status": 401,
            "instance": "urn:nmr-api:request:test",
            "request_id": "request:test",
            "code": "signature_expired",
            "detail": "private-detail-canary",
        }

        for (changes, known) in (
            ({}, True),
            ({"status": 404}, False),
            ({"code": "private-code-canary"}, False),
            ({"type": "urn:foreign"}, False),
            (
                {
                    "request_id": (
                        "bad\n"
                        "request"
                    )
                },
                False
            ),
        ):
            body = json.dumps(document | changes).encode()
            transport = Transport(reply(body, status=401, headers=response_headers(status=401, request_id=b"request:test")))

            with self.subTest(changes=changes), self.assertRaises(ApiFailure) as caught:
                await self.api(transport).job_input(facts(), analysis_kind_ref="nmr_prediction")

            yield caught.exception, known

        duplicate = json.dumps(document)[:-1] + ',"code":"signature_expired"}'
        transport = Transport(reply(duplicate.encode(), status=401, headers=response_headers(status=401, request_id=b"request:test")))

        with self.assertRaises(ApiFailure) as caught:
            await self.api(transport).job_input(facts(), analysis_kind_ref="nmr_prediction")

        yield caught.exception, False

    async def test_problem_reason_is_local_and_rejects_unknown_or_ambiguous_problems(self):
        async for (failure, known) in self._problem_read_failures():

            # silent-defaults: allow-contract -- Only the known correlated problem obtains the reviewed local code.
            self.assertEqual(failure.problem_code, "signature_expired" if known else None)

            # Header correlation survives rejected/unknown body diagnostics.
            self.assertEqual(failure.request_id, "request:test")

            if known:
                self.assertIn("expired", str(failure))
                self.assertIn("clock", str(failure))

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_problem_diagnostics_omit_server_content(self):
        async for (failure, _known) in self._problem_read_failures():
            self.assertNotIn("private-", "".join(traceback.format_exception(failure)))
