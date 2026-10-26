"""A real Unix tunnel preserves origin TLS, signatures and private request bytes."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse

from provider_core.signing import SigningIdentity, signed_request
from provider_core.transport import HttpTransport
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.unix_proxy import UnixProxy


class UnixTunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_signed_request_and_destination_sni_cross_the_unix_tunnel_once(self):
        async with TlsPeer("success") as peer, UnixProxy(peer.origin, "tunnel") as proxy:
            origin = peer.origin.replace("127.0.0.1", "localhost")
            key = Ed25519PrivateKey.generate()
            request = signed_request(
                SigningIdentity(origin, "credential:test", key),
                method="GET",
                path="/input",
                query=b"job=a%2Fb",
                body=None,
                created=1700000000,
                nonce=bytes(16),
            )
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5, proxy_socket=proxy.path)
            with patch("httpcore.AnyIOBackend.connect_tcp", new=AsyncMock(side_effect=AssertionError("direct TCP attempted"))):
                response = await transport.exchange(request, maximum_bytes=4)
            await transport.retire()
            self.assertEqual(response.body, b"data")
            self.assertEqual(peer.server_names, ["localhost"])
            self.assertEqual(len(peer.requests), 1)
            self.assertEqual(len(proxy.headers), 1)
            headers = {
                name.lower(): value
                for line in peer.requests[0].split(b"\r\n")[1:]
                if line
                for name, value in [line.split(b": ", 1)]
            }
            self.assertEqual(headers[b"host"].decode(), origin.removeprefix("https://"))
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
            key.public_key().verify(parse(headers[b"signature"], tltype="dictionary")["sig1"][0], base)
            self.assertTrue(proxy.headers[0].startswith(b"CONNECT localhost:"))
            self.assertNotIn(b"signature", proxy.headers[0].lower())
            await asyncio.wait_for(proxy.closed.wait(), 1)
