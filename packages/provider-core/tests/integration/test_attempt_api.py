"""Attempt readback crosses verified TLS and retires the actual connection."""

import asyncio
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import ATTEMPT_REF, observation


class AttemptReadIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_job_does_not_erase_in_progress_attempt(self):
        async with api_tls_peer("success", response_body=observation(job_state="closed")) as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            api = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            result = await api.read_attempt(ATTEMPT_REF, job_ref="job:example")
            self.assertEqual((result.state, result.job_state), ("in_progress", "closed"))
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertEqual(len(peer.requests), 1)
            self.assertTrue(
                peer.requests[0].startswith(b"GET /provider/v1/execution-attempts/" + ATTEMPT_REF.encode() + b" HTTP/1.1\r\n")
            )
            await transport.retire()
