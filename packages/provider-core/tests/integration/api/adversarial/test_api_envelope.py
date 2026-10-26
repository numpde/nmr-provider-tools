"""Actual TLS metadata remains useful when the body or API environment fails."""

import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api import response_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.job_input import facts, response


class ApiMetadataTlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_correlation_survives_foreign_environment_bad_json_and_failed_body(self):
        for mode, topology, body in (
            ("success", b"web", response()),
            ("success", b"dev-local", b'{"PRIVATE":'),
            ("disconnect", b"dev-local", b""),
            ("body-stall", b"dev-local", b""),
        ):
            with self.subTest(mode=mode, topology=topology):
                headers = response_headers(topology=topology, request_id=b"request:tls")
                async with api_tls_peer(mode, response_body=body, response_headers=headers) as peer:
                    api = ProviderAPI(
                        SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                        HttpTransport(trust=peer.trust, exchange_seconds=0.3, retirement_seconds=0.3),
                        expected_topology="dev-local",
                    )
                    try:
                        with self.assertRaises(ApiFailure) as caught:
                            await api.job_input(facts(), analysis_kind_ref="nmr_predict")
                        self.assertEqual((caught.exception.status, caught.exception.request_id), (200, "request:tls"))
                        self.assertIsNone(caught.exception.problem_code)
                        self.assertNotIn("PRIVATE", str(caught.exception))
                        self.assertEqual(len(peer.requests), 1)
                    finally:
                        await api.retire()
