"""One actual TLS exchange binds loaded credentials, body digest and acceptance."""

import asyncio
import hashlib
import unittest

from http_sf import parse

from provider_core.api import ProviderAPI
from provider_core.credential import parse_provider_credential
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.credential import document, encoded
from tests.fakes.hello import announcement, receipt


class SignedHelloTests(unittest.IsolatedAsyncioTestCase):
    async def test_loaded_credential_signs_one_current_announcement(self):
        credential = parse_provider_credential(encoded(document()))
        async with api_tls_peer("success", response_body=receipt()) as peer:
            api = ProviderAPI(
                SigningIdentity(peer.origin, credential.credential_ref, credential.private_key),
                HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5),
                expected_topology="dev-local",
            )
            try:
                accepted = await api.publish_hello(**announcement())
                await asyncio.wait_for(peer.closed.wait(), 1)
                self.assertEqual(accepted.provider_ref, credential.provider_ref)
                self.assertEqual(len(peer.requests), 1)
                request, body = peer.requests[0], peer.request_bodies[0]
                self.assertEqual(request.split(b"\r\n")[0], b"POST /provider/v1/hello HTTP/1.1")
                headers = {
                    name.lower(): value.strip()
                    for line in request.split(b"\r\n")[1:]
                    if b":" in line
                    for name, value in [line.split(b":", 1)]
                }
                self.assertEqual(
                    parse(headers[b"content-digest"], tltype="dictionary")["sha-256"][0], hashlib.sha256(body).digest()
                )
                base = b"\n".join(
                    [
                        b'"@method": POST',
                        b'"@authority": ' + peer.origin.removeprefix("https://").encode(),
                        b'"@path": /provider/v1/hello',
                        b'"@query": ?',
                        b'"content-type": application/json',
                        b'"content-digest": ' + headers[b"content-digest"],
                        b'"@signature-params": ' + headers[b"signature-input"].split(b"=", 1)[1],
                    ]
                )
                credential.private_key.public_key().verify(parse(headers[b"signature"], tltype="dictionary")["sig1"][0], base)
                self.assertNotIn(b"authorization", headers)
            finally:
                await api.retire()
