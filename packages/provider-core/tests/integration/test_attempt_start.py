"""Exact retained starts cross TLS; cancellation keeps admission unresolved."""

import asyncio
import unittest
from hashlib import sha256

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import retained_start, start_response


class AttemptStartIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_body_and_fresh_signatures_reach_tls_peer_on_replay(self):
        async with api_tls_peer("success", response_bodies=[start_response(), start_response(state="expired", replayed=True)]) as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            key = Ed25519PrivateKey.generate()
            api = ProviderAPI(SigningIdentity(peer.origin, "credential:test", key), transport, expected_topology="dev-local")
            body = retained_start()
            for _ in range(2):
                result = await api.start_attempt(
                    body, job_ref="job:example", analysis_kind_ref="nmr_predict", provider_ref="provider:example"
                )
                await asyncio.wait_for(peer.closed.wait(), 1)
                peer.closed.clear()
            self.assertEqual((result.state, result.replayed), ("expired", True))
            self.assertEqual(peer.request_bodies, [body, body])
            signatures = []
            for request in peer.requests:
                lines = request.split(b"\r\n")
                self.assertEqual(lines[0], b"POST /provider/v1/execution-attempts/start HTTP/1.1")
                headers = {
                    name.lower(): value.strip() for line in lines[1:] if b":" in line for name, value in [line.split(b":", 1)]
                }
                self.assertEqual(parse(headers[b"content-digest"], tltype="dictionary")["sha-256"][0], sha256(body).digest())
                parameters = headers[b"signature-input"].split(b"=", 1)[1]
                base = b"\n".join(
                    [
                        b'"@method": POST',
                        b'"@authority": ' + peer.origin.removeprefix("https://").encode(),
                        b'"@path": /provider/v1/execution-attempts/start',
                        b'"@query": ?',
                        b'"content-type": application/json',
                        b'"content-digest": ' + headers[b"content-digest"],
                        b'"@signature-params": ' + parameters,
                    ]
                )
                signature = parse(headers[b"signature"], tltype="dictionary")["sig1"][0]
                key.public_key().verify(signature, base)
                signatures.append(signature)
            self.assertNotEqual(*signatures)
            await transport.retire()

    async def test_cancel_after_received_start_retains_uncertainty_after_closure(self):
        async with api_tls_peer("headers-stall") as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
            api = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                transport,
                expected_topology="dev-local",
            )
            request = asyncio.create_task(
                api.start_attempt(
                    retained_start(), job_ref="job:example", analysis_kind_ref="nmr_predict", provider_ref="provider:example"
                )
            )
            await asyncio.wait_for(peer.received.wait(), 1)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertTrue(api.last_failure.attempt_mutation_may_have_committed)
            self.assertIs(api.last_failure.cause, transport.last_failure)
            self.assertEqual(peer.request_bodies, [retained_start()])
            await transport.retire()
