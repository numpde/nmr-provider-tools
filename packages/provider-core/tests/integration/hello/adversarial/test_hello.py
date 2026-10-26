"""Lost or interrupted announcements are not automatically repeated."""

import asyncio
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.hello import announcement, receipt


class UnconfirmedHelloTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_reply_allows_only_an_explicit_new_current_snapshot(self):
        async with api_tls_peer("success", response_bodies=[None, receipt()]) as peer:
            api = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5),
                expected_topology="dev-local",
            )
            try:
                with self.assertRaises(ApiFailure) as caught:
                    await api.publish_hello(**announcement())
                self.assertIn("acceptance is unconfirmed", " ".join(caught.exception.__notes__))
                self.assertEqual(len(peer.requests), 1)
                self.assertIsNotNone(caught.exception.cause)
                await api.retire()
                await api.publish_hello(**announcement(display_name="Current replacement", analysis_offerings=()))
                self.assertEqual(len(peer.requests), 2)
                self.assertNotEqual(peer.request_bodies[0], peer.request_bodies[1])
                self.assertIn(b'"analysis_offerings":[]', peer.request_bodies[1])
            finally:
                await api.retire()

    async def test_cancelled_send_keeps_failure_evidence_and_confirms_socket_cleanup(self):
        async with api_tls_peer("headers-stall") as peer:
            api = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5),
                expected_topology="dev-local",
            )
            owner = object()
            task = asyncio.create_task(api.publish_hello(**announcement(), owner=owner))
            try:
                await asyncio.wait_for(peer.received.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsInstance(api.last_failure.cause.primary, asyncio.CancelledError)
                self.assertIn("acceptance is unconfirmed", " ".join(api.last_failure.__notes__))
                self.assertEqual(len(peer.requests), 1)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await api.retire(owner=owner)
            await asyncio.wait_for(peer.closed.wait(), 1)
