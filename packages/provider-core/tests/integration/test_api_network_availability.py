"""Actual TLS EOF and controlled resolver failures are availability, not effects."""

import asyncio
import json
from pathlib import Path
import socket
import ssl
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from tests.fakes.api import response_headers
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import encoded, membership


class ApiNetworkAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def api(self, origin, trust):
        transport = HttpTransport(trust=trust,exchange_seconds=1,retirement_seconds=0.2)
        return ProviderAPI(SigningIdentity(origin,"credential:test",Ed25519PrivateKey.generate()),transport,
                           expected_topology="dev-local")

    async def test_tls_handshake_eof_allows_same_api_to_recover_with_full_verification(self):
        async with TlsPeer("success",response_body=encoded(membership()),response_headers=response_headers()) as peer:
            port=peer.server.sockets[0].getsockname()[1]
            peer.server.close()
            await peer.server.wait_closed()
            async def eof_after_client_hello(reader,writer):
                peer.handlers.add(asyncio.current_task())
                header=await reader.readexactly(5)
                await reader.readexactly(int.from_bytes(header[3:5],"big"))
                writer.close()
                await writer.wait_closed()
            peer.server=await asyncio.start_server(eof_after_client_hello,"127.0.0.1",port)
            api=self.api(peer.origin,peer.trust)
            with self.assertRaises(ApiFailure) as caught:
                await api.uploads("job:example")
            failure=caught.exception
            self.assertTrue(api.retirement_confirmed)
            print(json.dumps({"primary":type(failure.cause.primary).__name__,
                              "context":type(failure.cause.primary.__context__).__name__,
                              "retry":api.can_retry_read(failure)}),flush=True)
            self.assertTrue(api.can_retry_read(failure))
            peer.server.close()
            await peer.server.wait_closed()
            trust=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            trust.load_cert_chain(Path(peer.scratch.name)/"cert.pem",Path(peer.scratch.name)/"key.pem")
            peer.closed=asyncio.Event()
            peer.server=await asyncio.start_server(peer.handle,"127.0.0.1",port,ssl=trust,
                                                   ssl_handshake_timeout=0.5,ssl_shutdown_timeout=0.5)
            result=await api.uploads("job:example")
            self.assertEqual(result.job_ref,"job:example")
            self.assertEqual(len(peer.requests),1)
            await asyncio.wait_for(peer.closed.wait(),1)

    async def test_noname_dns_from_actual_resolver_is_recoverable(self):
        original=socket.gaierror(socket.EAI_NONAME,"controlled original resolver outage")
        api=self.api("https://api.fixture.example",ssl.create_default_context())
        with patch("socket.getaddrinfo",side_effect=original) as resolver:
            with self.assertRaises(ApiFailure) as caught:
                await api.uploads("job:example")
        self.assertTrue(resolver.called)
        self.assertTrue(api.retirement_confirmed)
        self.assertTrue(api.can_retry_read(caught.exception))

    async def test_configured_missing_unix_proxy_recovers_only_through_original_path(self):
        from tests.fakes.unix_proxy import UnixProxy
        async with TlsPeer("success", response_body=encoded(membership()), response_headers=response_headers()) as peer:
            async with UnixProxy(peer.origin, "tunnel") as proxy:
                proxy.server.close()
                await proxy.server.wait_closed()
                Path(proxy.path).unlink()
                transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=.2, proxy_socket=proxy.path)
                api = ProviderAPI(SigningIdentity(peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                                  transport, expected_topology="dev-local")
                with patch("socket.getaddrinfo", side_effect=AssertionError("configured Unix route must not resolve TCP")):
                    with self.assertRaises(ApiFailure) as caught:
                        await api.uploads("job:example")
                    failure = caught.exception
                    self.assertTrue(api.retirement_confirmed)
                    self.assertEqual(failure.cause.network_io.stage, "connect_unix")
                    self.assertEqual(failure.cause.network_io.unix_path, proxy.path)
                    self.assertTrue(api.can_retry_read(failure))
                    proxy.server = await asyncio.start_unix_server(proxy.handle, proxy.path, limit=16384)
                    result = await api.uploads("job:example")
                    self.assertEqual(result.job_ref, "job:example")
                    self.assertEqual(len(proxy.headers), 1)
                    self.assertEqual(len(peer.requests), 1)
                    await asyncio.wait_for(peer.closed.wait(), 1)
                    await asyncio.wait_for(proxy.closed.wait(), 1)

    async def test_real_certificate_validation_is_not_availability(self):
        async with TlsPeer("success", response_body=encoded(membership()), response_headers=response_headers()) as peer:
            api = self.api(peer.origin, ssl.create_default_context())
            with self.assertRaises(ApiFailure) as caught:
                await api.uploads("job:example")
            self.assertTrue(api.retirement_confirmed)
            self.assertEqual(caught.exception.cause.network_io.stage, "start_tls")
            self.assertFalse(api.can_retry_read(caught.exception))
            self.assertEqual(peer.requests, [])
