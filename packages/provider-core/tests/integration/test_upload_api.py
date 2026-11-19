"""Upload API observations use actual TLS without following grant URLs."""

import asyncio
import unittest
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.adapters.nmr.v1.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, membership


class UploadApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _membership_and_grant_observations(self):
        for (granting, document) in ((False, membership()), (True, grant())):
            with self.subTest(granting=granting):
                async with api_tls_peer("success", response_body=encoded(document)) as peer:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)

                    api = ProviderAPI(
                        SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                        transport,
                        expected_topology="dev-local",
                    )

                    observed = await api.read_grant("job:example", UPLOAD_REF) if granting else await api.uploads("job:example")

                    self.assertEqual(observed.job_ref, "job:example")
                    await asyncio.wait_for(peer.closed.wait(), 1)
                    self.assertEqual(len(peer.requests), 1)

                    # silent-defaults: allow-contract -- The chosen operation determines the expected mutation POST or read GET.
                    self.assertTrue(peer.requests[0].startswith(b"POST " if granting else b"GET "))

                    await transport.retire()

                    yield peer.requests[0]

    async def test_membership_and_grant_observation_close_api_socket(self):
        async for request in self._membership_and_grant_observations():
            self.assertNotIn(b"Content-Digest:", request)

    @unittest.skipIf(os.environ.get("PRIVACY_TESTS", "1") == "0", "Privacy tests disabled")
    async def test_membership_and_grant_requests_omit_store_bearer(self):
        async for request in self._membership_and_grant_observations():
            self.assertNotIn(b"private-bearer-canary", request)

    async def test_cancelled_grant_retains_uncertainty_after_actual_socket_closes(self):
        async with api_tls_peer("headers-stall") as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)

            api = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )

            request = asyncio.create_task(api.read_grant("job:example", UPLOAD_REF))

            await asyncio.wait_for(peer.received.wait(), 1)
            request.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await request

            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertTrue(api.last_failure.issuance_may_have_committed)
            self.assertIs(api.last_failure.cause, transport.last_failure)
            self.assertEqual(len(peer.requests), 1)
            await transport.retire()
