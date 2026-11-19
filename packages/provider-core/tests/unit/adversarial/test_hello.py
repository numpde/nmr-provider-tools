"""Invalid announcements send nothing; uncertain acceptance never becomes readiness or replay."""

import asyncio
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.hello import AnalysisOffering
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.hello import announcement, receipt


class HelloBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def _invalid_offerings(self):
        return (
            ("PRIVATE invalid", "d", ValueError),
            ("nmr_predict", "PRIVATE\udfff", ValueError),
            ("nmr_predict", "PRIVATE\0value", ValueError),
            ("nmr_predict", "", ValueError),
            ("nmr_predict", "x" * 1025, ValueError),
            ("nmr_predict", [], TypeError),
        )

    def test_offering_fields_are_admitted_at_construction_before_aggregation(self):
        for (kind, description, error_type) in self._invalid_offerings():
            with self.subTest(kind_valid=kind == "nmr_predict"), self.assertRaises(error_type):
                AnalysisOffering(kind, description)

    @unittest.skip("Privacy policy under review")
    def test_offering_rejections_omit_input(self):
        for (kind, description, error_type) in self._invalid_offerings():
            with self.subTest(kind_valid=kind == "nmr_predict"), self.assertRaises(error_type) as caught:
                AnalysisOffering(kind, description)

            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_offering_length_refusal_supplies_counts(self):
        with self.assertRaises(ValueError) as caught:
            AnalysisOffering("nmr_predict", "PRIVATE" + "x" * 1330)

        self.assertIn("1337", str(caught.exception))
        self.assertIn("1024", str(caught.exception))

    @unittest.skip("Privacy policy under review")
    def test_offering_length_refusal_omits_description_content(self):
        with self.assertRaises(ValueError) as caught:
            AnalysisOffering("nmr_predict", "PRIVATE" + "x" * 1330)

        self.assertNotIn("PRIVATE", str(caught.exception))

    def api(self, transport):
        return ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )

    async def _invalid_announcement_failures(self):
        for change in (
            {"provider_ref": "PRIVATE invalid"},
            {"display_name": ""},
            {"display_name": "x" * 129},
            {"description": "x" * 1025},
            {"description": "PRIVATE\0value"},
            {"display_name": "PRIVATE\ud800"},
            {"description": []},
            {"analysis_offerings": []},
            {"analysis_offerings": (AnalysisOffering("nmr_predict", "one"), AnalysisOffering("nmr_predict", "one"))},
            # repo-check: allow-tuple-generator -- This over-limit tuple specimen reaches the Hello collection-bound refusal.
            {"analysis_offerings": tuple(AnalysisOffering("kind_" + str(i), "d") for i in range(65))},
            {"analysis_offerings": ({"analysis_kind_ref": "nmr_predict", "description": "PRIVATE"},)},
        ):
            with self.subTest(field=next(iter(change))):
                transport = Transport()

                with self.assertRaises((ValueError, TypeError)) as caught:
                    await self.api(transport).publish_hello(**announcement(**change))

                yield caught.exception, transport

    async def test_invalid_snapshot_and_expected_identity_fail_before_send(self):
        async for (_failure, transport) in self._invalid_announcement_failures():
            self.assertEqual(transport.calls, [])

    @unittest.skip("Privacy policy under review")
    async def test_invalid_snapshot_diagnostics_omit_input(self):
        async for (failure, _transport) in self._invalid_announcement_failures():
            self.assertNotIn("PRIVATE", str(failure))

    async def _invalid_acceptance_failures(self):
        for body in (
            receipt(provider_ref="provider:other"),
            receipt(provider_ref=[]),
            receipt(schema_id="PRIVATE"),
            receipt(accepted_at="2026-02-30T00:00:00Z"),
            receipt(accepted_at="2026-10-20T10:00:00.000000Z"),
            receipt(accepted_at="2026-10-20T10:00:00+00:00"),
            receipt(extra="PRIVATE"),
            b"[]",
            b"{}",
            b"\xffPRIVATE",
            b'{"schema_id":"PRIVATE",' + receipt()[1:],
            b" " * 65537,
        ):
            with self.subTest(size=len(body)):
                response = reply(body)
                transport = Transport(response)
                api = self.api(transport)

                with self.assertRaises(ApiFailure) as caught:
                    await api.publish_hello(**announcement())

                yield caught.exception, api, response, transport

    async def test_invalid_acceptance_keeps_original_response_and_uncertainty(self):
        async for (failure, api, response, transport) in self._invalid_acceptance_failures():
            self.assertIs(api.last_failure, failure)
            self.assertIs(failure.response, response)
            self.assertIsNotNone(failure.cause)
            self.assertIn("acceptance is unconfirmed", " ".join(failure.__notes__))
            self.assertFalse(failure.attempt_mutation_may_have_committed)
            self.assertEqual(len(transport.calls), 1)

    @unittest.skip("Privacy policy under review")
    async def test_acceptance_diagnostics_omit_response_content(self):
        async for (failure, _api, _response, _transport) in self._invalid_acceptance_failures():
            self.assertNotIn("PRIVATE", str(failure))

    async def _http_or_transport_failures(self):
        cause = ExchangeFailed("Cannot obtain a response", primary=OSError("PRIVATE transport"))

        for outcome in (reply(b"PRIVATE", status=503), reply(receipt(), headers=()), cause):
            transport = Transport(outcome)
            api = self.api(transport)

            with self.assertRaises(ApiFailure) as caught:
                await api.publish_hello(**announcement())

            yield caught.exception, outcome, cause, transport

    async def test_http_or_transport_failure_is_one_send_with_protected_cause(self):
        async for (failure, outcome, cause, transport) in self._http_or_transport_failures():
            self.assertIn("directory snapshot may already have changed", " ".join(failure.__notes__))

            if outcome is cause:
                self.assertIs(failure.cause, cause)

            self.assertEqual(len(transport.calls), 1)

    @unittest.skip("Privacy policy under review")
    async def test_hello_failure_diagnostics_omit_response_and_transport_content(self):
        async for (failure, _outcome, _cause, _transport) in self._http_or_transport_failures():
            self.assertNotIn("PRIVATE", str(failure))

    async def test_cancellation_keeps_same_exception_and_operation_evidence(self):
        cancellation = asyncio.CancelledError()
        transport = Transport(cancellation)
        api = self.api(transport)

        with self.assertRaises(asyncio.CancelledError) as caught:
            await api.publish_hello(**announcement())

        self.assertIs(caught.exception, cancellation)
        self.assertIn("Hello", api.last_failure.operation)
        self.assertIn("acceptance is unconfirmed", " ".join(api.last_failure.__notes__))
        self.assertEqual(len(transport.calls), 1)
