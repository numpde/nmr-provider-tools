"""Progress uses the signed PUT route; interrupted dispatch remains unresolved."""

import asyncio
import json
import unittest
from hashlib import sha256

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import ATTEMPT_REF, progress_response


class AttemptProgressIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_received_put_and_digest_match_current_observation(self):
        async with api_tls_peer("success", response_body=progress_response(phase="running")) as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            api = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            result = await api.progress_attempt(ATTEMPT_REF, phase="running", condition_code=None)
            self.assertEqual(result.phase, "running")
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertEqual(len(peer.requests), 1)
            self.assertTrue(
                peer.requests[0].startswith(
                    b"PUT /provider/v1/execution-attempts/" + ATTEMPT_REF.encode() + b"/progress HTTP/1.1\r\n"
                )
            )
            headers = {
                name.lower(): value.strip()
                for line in peer.requests[0].split(b"\r\n")[1:]
                if b":" in line
                for name, value in [line.split(b":", 1)]
            }
            body = peer.request_bodies[0]
            self.assertEqual(
                json.loads(body),
                {"schema_id": "nmr.provider.execution_attempt_progress_request.v1", "phase": "running", "condition_code": None},
            )
            self.assertEqual(parse(headers[b"content-digest"], tltype="dictionary")["sha-256"][0], sha256(body).digest())
            self.assertIn(b"signature", headers)
            await transport.retire()

    async def test_cancel_after_progress_dispatch_retains_uncertainty(self):
        async with api_tls_peer("headers-stall") as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
            api = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            task = asyncio.create_task(api.progress_attempt(ATTEMPT_REF, phase="preparing", condition_code=None))
            await asyncio.wait_for(peer.received.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertTrue(api.last_failure.attempt_mutation_may_have_committed)
            self.assertIs(api.last_failure.cause, transport.last_failure)
            self.assertEqual(len(peer.requests), 1)
            await transport.retire()
