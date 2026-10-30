"""An isolated full proxy reports a retryable discovery failure after retirement."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from provider_core.api import ApiFailure
from provider_core.provider_proxy import ServicePolicy, ServiceProxyServer
from provider_core.transport import HttpTransport
from tests.fakes.api import api
from tests.fakes.api_tls import api_tls_peer


class ProxyCapacityRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_proxy_discovery_is_retryable_after_exchange_retirement(self):
        kind = 'mol_from_1h_spectrum_formula'

        async def response(_request, _body):
            return json.dumps({'schema_id': 'nmr.provider.jobs.list.response.v1',
                'analysis_kind_ref': kind, 'has_provider_execution_attempt': False,
                'next_cursor': None, 'jobs': []}).encode()

        async with api_tls_peer('success', response_factory=response) as peer:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entered, exited = threading.Event(), threading.Event()

                class ObservedProxy(ServiceProxyServer):
                    def process_request_thread(self, request, address):
                        entered.set()
                        try:
                            super().process_request_thread(request, address)
                        finally:
                            exited.set()

                policy = ServicePolicy.admit('mapped', [urlsplit(peer.origin).netloc],
                                             ['127.0.0.1=127.0.0.1'])
                with ObservedProxy(root / 'proxy.sock', 'lo', policy, status_directory=root) as proxy:
                    proxy._slots.set_limit(1)
                    thread = threading.Thread(target=proxy.serve_forever, kwargs={'poll_interval': 0.01})
                    thread.start()
                    writer = client = recovered = None
                    try:
                        _reader, writer = await asyncio.open_unix_connection(str(root / 'proxy.sock'))
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2), 'First connection did not occupy the proxy')
                        transport = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=1,
                                                  proxy_socket=str(root / 'proxy.sock'))
                        client = api(transport, peer.origin)
                        with self.assertRaises(ApiFailure) as caught:
                            await client.jobs(analysis_kind_ref=kind, has_provider_execution_attempt=False, limit=1, cursor=None)
                        await client.retire()
                        self.assertTrue(client.retirement_confirmed)
                        retryable = client.can_retry_job_discovery(caught.exception)
                        writer.close()
                        await writer.wait_closed()
                        writer = None
                        self.assertTrue(await asyncio.to_thread(exited.wait, 2), 'Original proxy connection did not retire')
                        second = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=1,
                                               proxy_socket=str(root / 'proxy.sock'))
                        recovered = api(second, peer.origin)
                        page = await recovered.jobs(analysis_kind_ref=kind, has_provider_execution_attempt=False,
                                                    limit=1, cursor=None)
                        self.assertEqual(page.jobs, ())
                        self.assertTrue(retryable,
                            'Temporary proxy capacity exhaustion was not retryable after exchange retirement')
                    finally:
                        cleanup_errors = []
                        async def settle(operation):
                            try:
                                await operation()
                            except BaseException as error:
                                cleanup_errors.append(error)
                        if writer is not None:
                            async def close_writer():
                                writer.close()
                                await writer.wait_closed()
                            await settle(close_writer)
                        if client is not None:
                            await settle(client.retire)
                        if recovered is not None:
                            await settle(recovered.retire)
                        await settle(lambda: asyncio.to_thread(proxy.shutdown))
                        await settle(lambda: asyncio.to_thread(thread.join, 2))
                        if thread.is_alive():
                            cleanup_errors.append(RuntimeError('Isolated proxy listener did not retire'))
                        if cleanup_errors:
                            raise BaseExceptionGroup('Isolated test cleanup failed', cleanup_errors)
