"""Actual TLS sockets establish bounded reads and closure beyond mocked clients."""

import asyncio
import unittest

import httpcore
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse

from provider_core.signing import SigningIdentity, signed_request
from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.tls_peer import TlsPeer


class TransportPeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_signature_verifies_against_request_received_over_tls(self):
        key = Ed25519PrivateKey.generate()
        async with TlsPeer("success") as peer:
            request = signed_request(
                SigningIdentity(peer.origin, "credential:test", key),
                method="GET",
                path="/input",
                query=b"job=a%2Fb",
                body=None,
                created=1700000000,
                nonce=bytes(16),
            )
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            await transport.exchange(request, maximum_bytes=4)
            await asyncio.wait_for(peer.closed.wait(), 1)
            lines = peer.requests[0].split(b"\r\n")
            self.assertEqual(lines[0], b"GET /input?job=a%2Fb HTTP/1.1")
            headers = dict(line.split(b": ", 1) for line in lines[1:] if line)
            headers = {name.lower(): value for name, value in headers.items()}
            self.assertEqual(headers[b"host"].decode(), peer.origin.removeprefix("https://"))
            self.assertNotIn(b"content-digest", headers)
            parameters = headers[b"signature-input"].removeprefix(b"sig1=")
            base = b"\n".join(
                [
                    b'"@method": GET',
                    b'"@authority": ' + headers[b"host"],
                    b'"@path": /input',
                    b'"@query": ?job=a%2Fb',
                    b'"@signature-params": ' + parameters,
                ]
            )
            signature = parse(headers[b"signature"], tltype="dictionary")["sig1"][0]
            key.public_key().verify(signature, base)

    async def test_complete_response_and_redirect_do_not_leave_a_connection(self):
        for mode, status, body in (("success", 200, b"data"), ("redirect", 302, b"")):
            with self.subTest(mode=mode):
                async with TlsPeer(mode) as peer:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
                    response = await transport.exchange(httpx.Request("GET", peer.origin + "/input"), maximum_bytes=100)
                    self.assertEqual((response.status, response.body), (status, body))
                    await asyncio.wait_for(peer.closed.wait(), 1)
                    self.assertEqual(len(peer.requests), 1)
                    await transport.retire()

    async def test_stalls_and_trickle_share_one_aggregate_deadline(self):
        for mode in ("tls-stall", "headers-stall", "body-stall", "trickle"):
            with self.subTest(mode=mode):
                async with TlsPeer(mode) as peer:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=0.15, retirement_seconds=0.3)
                    started = asyncio.get_running_loop().time()
                    with self.assertRaises(ExchangeFailed) as caught:
                        await transport.exchange(httpx.Request("GET", peer.origin + "/input"), maximum_bytes=100)
                    self.assertIsInstance(caught.exception.primary, TimeoutError)
                    self.assertTrue(peer.received.is_set())
                    self.assertLess(asyncio.get_running_loop().time() - started, 0.7)
                    await asyncio.wait_for(peer.closed.wait(), 1)
                    await transport.retire()

    async def test_cancellation_closes_the_actual_socket(self):
        for mode in ("tls-stall", "headers-stall", "body-stall"):
            with self.subTest(mode=mode):
                async with TlsPeer(mode) as peer:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
                    task = asyncio.create_task(
                        transport.exchange(httpx.Request("GET", peer.origin + "/input"), maximum_bytes=100)
                    )
                    await asyncio.wait_for(peer.received.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await asyncio.wait_for(peer.closed.wait(), 1)
                    await transport.retire()

    async def test_midbody_disconnect_never_yields_partial_response(self):
        async with TlsPeer("disconnect") as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
            with self.assertRaises(ExchangeFailed) as caught:
                await transport.exchange(httpx.Request("GET", peer.origin + "/input"), maximum_bytes=100)
            self.assertIsInstance(caught.exception.primary, httpcore.RemoteProtocolError)
            self.assertTrue(peer.received.is_set())
            await transport.retire()
