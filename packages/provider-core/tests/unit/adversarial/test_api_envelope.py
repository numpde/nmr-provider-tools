"""Deployment and correlation checks cannot turn incomplete replies into authority."""

import json
import unittest
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.adapters.nmr.v1.signing import SigningIdentity
from provider_core.transport import ExchangeFailed, HttpResponseHead
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.hello import announcement, receipt
from tests.fakes.job_input import facts, response
from tests.fakes.uploads import UPLOAD_REF

HEADERS = ((b"content-type", b"application/json"), (b"cache-control", b"no-store"))
MARKER = (b"nmr-api-topology", b"dev-local")
CORRELATION = (b"x-request-id", b"request:header")


class ApiEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    def identity(self):
        return SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate())

    def api(self, transport):
        return ProviderAPI(self.identity(), transport, expected_topology="dev-local")

    def test_explicit_topology_is_required_before_any_exchange(self):
        transport = Transport()

        with self.assertRaises(TypeError):
            ProviderAPI(self.identity(), transport)

        for (topology, error_type) in ((None, TypeError), ("run", ValueError), ("PRIVATE", ValueError), (True, TypeError), ([], TypeError)):
            with self.subTest(topology_type=type(topology).__name__), self.assertRaises(error_type) as caught:
                ProviderAPI(self.identity(), transport, expected_topology=topology)

        self.assertEqual(transport.calls, [])

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    def test_topology_rejection_diagnostics_omit_input(self):
        with self.assertRaises(ValueError) as caught:
            ProviderAPI(self.identity(), Transport(), expected_topology="PRIVATE")

        self.assertNotIn("PRIVATE", str(caught.exception))

    async def test_missing_foreign_or_duplicate_topology_rejects_valid_input_and_hello(self):
        for markers in ((), ((b"nmr-api-topology", b"web"),), (MARKER, MARKER)):
            for hello in (False, True):
                with self.subTest(markers=markers, hello=hello):
                    observed = reply(receipt() if hello else response(), headers=(*HEADERS, *markers, CORRELATION))
                    transport = Transport(observed)
                    api = self.api(transport)

                    with self.assertRaises(ApiFailure) as caught:
                        if hello:
                            await api.publish_hello(**announcement())
                        else:
                            await api.job_input(facts(), analysis_kind_ref="nmr_predict")

                    self.assertIs(caught.exception.response, observed)
                    self.assertEqual((caught.exception.status, caught.exception.request_id), (200, "request:header"))
                    self.assertIsNone(caught.exception.problem_code)
                    self.assertEqual(len(transport.calls), 1)

    async def _bad_body_failures(self):
        for (ids, expected) in (
            ((CORRELATION,), "request:header"),
            ((), None),
            (((b"X-Request-ID", b"PRIVATE invalid"),), None),
            ((CORRELATION, CORRELATION), None),
        ):
            observed = reply(b'{"PRIVATE":', headers=(*HEADERS, MARKER, *ids))

            with self.subTest(ids=ids), self.assertRaises(ApiFailure) as caught:
                await self.api(Transport(observed)).job_input(facts(), analysis_kind_ref="nmr_predict")

            yield caught.exception, expected, observed

    async def test_bad_body_keeps_header_correlation_but_ambiguous_ids_are_never_trusted(self):
        async for (failure, expected, observed) in self._bad_body_failures():
            self.assertEqual(failure.request_id, expected)
            self.assertIs(failure.response, observed)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_bad_body_diagnostics_omit_body_and_untrusted_headers(self):
        async for (failure, _expected, _observed) in self._bad_body_failures():
            self.assertNotIn("PRIVATE", str(failure))

    async def test_duplicate_request_ids_reject_an_otherwise_valid_success_body(self):
        observed = reply(response(), headers=(*HEADERS, MARKER, CORRELATION, CORRELATION))
        transport = Transport(observed)

        with self.assertRaises(ApiFailure) as caught:
            await self.api(transport).job_input(facts(), analysis_kind_ref="nmr_predict")

        self.assertIs(caught.exception.response, observed)
        self.assertIsNone(caught.exception.request_id)
        self.assertEqual(len(transport.calls), 1)

    async def test_late_head_is_diagnostic_and_never_downgrades_grant_uncertainty(self):
        cause = ExchangeFailed("Incomplete exchange", primary=OSError("PRIVATE"))
        api = self.api(Transport(cause))

        with self.assertRaises(ApiFailure) as caught:
            await api.read_grant("job:example", UPLOAD_REF)

        failure = caught.exception

        self.assertEqual((failure.status, failure.request_id), (None, None))
        self.assertTrue(failure.issuance_may_have_committed)
        cause.response_head = HttpResponseHead(401, (MARKER, CORRELATION))

        self.assertEqual((failure.status, failure.request_id), (401, "request:header"))
        self.assertTrue(failure.issuance_may_have_committed)
        self.assertIsNone(failure.problem_code)
        self.assertIs(failure.cause, cause)

    async def _correlated_refusals(self):
        body = (
            json
            .dumps(
                {
                    "type": "urn:nmr-api:problem:authentication-failed",
                    "title": "PRIVATE",
                    "status": 401,
                    "instance": "urn:nmr-api:request:example",
                    "request_id": "request:body",
                    "code": "signature_expired",
                    "detail": "PRIVATE",
                },
            )
            .encode()
        )

        for (topology, request_id, known) in (
            (b"dev-local", b"request:body", True),
            (b"dev-local", b"request:other", False),
            (b"web", b"request:body", False),
            (b"dev-local", None, False),
        ):
            transport = Transport(
                reply(body, status=401, headers=response_headers(topology=topology, request_id=request_id, status=401))
            )

            with self.subTest(topology=topology, request_id=request_id), self.assertRaises(ApiFailure) as caught:
                await self.api(transport).read_grant("job:example", UPLOAD_REF)

            yield caught.exception, known, request_id

    async def test_reviewed_refusal_requires_environment_and_matching_header_body_correlation(self):
        async for (failure, known, request_id) in self._correlated_refusals():
            # silent-defaults: allow-contract -- Only the known correlated problem obtains the reviewed local code.
            self.assertEqual(failure.problem_code, "signature_expired" if known else None)

            self.assertEqual(failure.issuance_may_have_committed, not known)

            # silent-defaults: allow-contract -- A missing request ID remains absent in the refusal specimen.
            self.assertEqual(failure.request_id, None if request_id is None else request_id.decode())

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_correlated_refusal_diagnostics_omit_problem_content(self):
        async for (failure, _known, _request_id) in self._correlated_refusals():
            self.assertNotIn("PRIVATE", str(failure))
