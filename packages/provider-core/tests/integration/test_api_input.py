"""Signed API reads traverse actual TLS before exact Job bytes are admitted."""

import asyncio
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.job_input import facts, response


class ApiInputIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_input_or_identity_rejection_after_socket_closure(self):
        for job_ref in ("job:example", "job:foreign"):
            with self.subTest(job_ref=job_ref):
                async with api_tls_peer("success", response_body=response(job_ref=job_ref)) as peer:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
                    api = ProviderAPI(
                        SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                        transport,
                        expected_topology="dev-local",
                    )
                    if job_ref == "job:example":
                        admitted = await api.job_input(facts(), analysis_kind_ref="nmr_prediction")
                        self.assertEqual(admitted.specification_bytes, b"private instructions")
                    else:
                        with self.assertRaises(ApiFailure) as caught:
                            await api.job_input(facts(), analysis_kind_ref="nmr_prediction")
                        self.assertIn("job_ref", str(caught.exception))
                    await asyncio.wait_for(peer.closed.wait(), 1)
                    self.assertEqual(len(peer.requests), 1)
                    self.assertIn(
                        b"GET /provider/v1/jobs/job:example/input?analysis_kind_ref=nmr_prediction HTTP/1.1", peer.requests[0]
                    )
                    self.assertIn(b"Signature:", peer.requests[0])
                    await transport.retire()
