"""A malformed CONNECT reply does not become evidence of proxy overload."""

import unittest

import httpcore
from provider_core.api import ApiFailure
from provider_core.transport import HttpTransport
from tests.fakes.api import api
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.unix_proxy import UnixProxy


class MalformedProxyResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_unheaded_malformed_proxy_reply_remains_nonretryable(self):
        async with api_tls_peer('success') as peer:
            async with UnixProxy(peer.origin, 'malformed') as proxy:
                transport = HttpTransport(trust=peer.trust, exchange_seconds=2,
                                          retirement_seconds=1, proxy_socket=proxy.path)
                client = api(transport, peer.origin)
                try:
                    with self.assertRaises(ApiFailure) as caught:
                        await client.jobs(analysis_kind_ref='mol_from_1h_spectrum_formula',
                                          has_provider_execution_attempt=False, limit=1, cursor=None)
                    await client.retire()
                    self.assertTrue(client.retirement_confirmed)
                    self.assertIs(type(caught.exception.cause.primary), httpcore.RemoteProtocolError)
                    self.assertIsNone(caught.exception.cause.proxy_connect_status)
                    self.assertIsNone(caught.exception.cause.response_head)
                    self.assertFalse(client.can_retry_job_discovery(caught.exception))
                    self.assertEqual(peer.requests, [])
                finally:
                    await client.retire()
