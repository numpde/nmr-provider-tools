"""Decoded exchanges retain wire accounting, original owners and raw-mode contracts."""
import asyncio
import gzip
import os
import ssl
import tempfile
import unittest
from unittest.mock import patch

import httpcore
import httpx
import provider_core.transport as m
from provider_core.decoded_body import DecodedBodyRejected


class Stream:
    def __init__(self, chunks, *, failure=None):
        self.chunks = chunks
        self.failure = failure
        self.closed = False
        self.iterated = False

    async def __aiter__(self):
        self.iterated = True
        for chunk in self.chunks:
            yield chunk
        if self.failure is not None:
            raise self.failure

    async def aclose(self):
        self.closed = True


class Client:
    # silent-defaults: allow-contract -- The ordinary synthetic response is HTTP 200 with no extra headers; each fault supplies its own evidence.
    def __init__(self, chunks, *, status=200, headers=(), failure=None, close_failure=None, close_gate=None):
        self.stream = Stream(chunks, failure=failure)
        self.response = httpcore.Response(status, headers=headers, content=self.stream)
        self.closed = False
        self.close_failure = close_failure
        self.close_gate = close_gate
        self.close_started = asyncio.Event()
        self.task = None

    async def handle_async_request(self, request):
        self.task = asyncio.current_task()
        return self.response

    async def aclose(self):
        self.close_started.set()
        if self.close_gate is not None:
            while not self.close_gate.is_set():
                try:
                    await self.close_gate.wait()
                # silent-defaults: allow-contract -- This specimen models physical cleanup that remains pending after observer cancellation.
                except asyncio.CancelledError:
                    # Model physical cleanup that cannot settle until released.
                    pass
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True


class TransportDecodedTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, **kwargs):
        return m.HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=.02, **kwargs)

    # silent-defaults: allow-contract -- The ordinary synthetic model URL is fixed; HTTP opt-in tests explicitly replace it.
    def request(self, url='https://model.example/chat'):
        return httpx.Request('POST', url, content=b'{}')

    async def decoded(self, transport, client, **kwargs):
        with patch.object(m.httpcore, 'AsyncConnectionPool', return_value=client):
            return await transport.exchange_decoded(self.request(), maximum_bytes=1024, error_maximum_bytes=128, **kwargs)

    async def test_gzip_concat_is_complete_and_headers_remain_original_after_cleanup(self):
        encoded = gzip.compress(b'first') + gzip.compress(b'second')
        headers = [(b'content-encoding', b'gzip'), (b'content-length', str(len(encoded)).encode())]
        client = Client([encoded[:7], encoded[7:]], headers=headers)
        charged = []
        transport = self.transport()
        response = await self.decoded(transport, client, account_bytes=charged.append)
        self.assertEqual(response.body, b'firstsecond')
        self.assertEqual(response.headers, tuple(headers))
        self.assertEqual(charged, [7, len(encoded)-7])
        self.assertTrue(client.closed and client.stream.closed and transport.retirement_confirmed)

    async def test_status_selects_both_caps_from_actual_received_head(self):
        for status, length, passes in ((201, 12, True), (200, 12, False), (503, 12, False), (503, 4, True)):
            with self.subTest(status=status, length=length):
                client = Client([b'x'*length], status=status)
                transport = self.transport()
                with patch.object(m.httpcore, 'AsyncConnectionPool', return_value=client):
                    operation = transport.exchange_decoded(self.request(), maximum_bytes=16, error_maximum_bytes=4, success_status=201)
                    if passes:
                        self.assertEqual((await operation).body, b'x'*length)
                    else:
                        with self.assertRaises(m.ExchangeFailed) as caught:
                            await operation
                        self.assertIs(type(caught.exception.primary), DecodedBodyRejected)
                        self.assertEqual(caught.exception.primary.reason, 'size_limit')
                        self.assertEqual(caught.exception.response_head.status, status)
                        self.assertIsNone(caught.exception.network_failure)

    async def test_wire_limit_charges_rejecting_chunk_before_any_decode(self):
        client = Client([b'abc', b'def'], headers=[(b'content-encoding', b'gzip')])
        transport = self.transport()
        charged = []
        decoder = unittest.mock.Mock()
        decoder.feed.return_value = b''
        with patch.object(m.httpcore, 'AsyncConnectionPool', return_value=client), patch.object(m, 'DecodedBody', return_value=decoder):
            with self.assertRaises(m.ExchangeFailed) as caught:
                await transport.exchange_decoded(self.request(), maximum_bytes=5, error_maximum_bytes=2, account_bytes=charged.append)
        self.assertEqual(charged, [3, 3])
        decoder.feed.assert_called_once_with(b'abc')
        decoder.finish.assert_not_called()
        self.assertEqual(caught.exception.primary.reason, 'size_limit')
        self.assertIn('encoded or decoded', str(caught.exception))
        self.assertTrue(transport.retirement_confirmed)

    async def test_head_refusal_precedes_codec_iteration_and_accounting(self):
        refusal = RuntimeError('private admission evidence')
        client = Client([b'private'], headers=[(b'content-encoding', b'unknown')])
        charged = []
        transport = self.transport()
        with patch.object(m, 'DecodedBody', side_effect=AssertionError('decoder constructed')):
            with self.assertRaises(m.ExchangeFailed) as caught:
                await self.decoded(transport, client, account_bytes=charged.append, before_body=lambda head: (_ for _ in ()).throw(refusal))
        self.assertIs(caught.exception.primary, refusal)
        self.assertEqual(caught.exception.response_head.status, 200)
        self.assertIsNone(caught.exception.network_failure)
        self.assertFalse(client.stream.iterated)
        self.assertEqual(charged, [])
        self.assertTrue(client.closed and client.stream.closed)

    async def test_invalid_codec_retains_head_and_cause_without_network_marker(self):
        client = Client([gzip.compress(b'body')[:-3]], headers=[(b'content-encoding', b'gzip'), (b'x-request-id', b'model-7')])
        transport = self.transport()
        with self.assertRaises(m.ExchangeFailed) as caught:
            await self.decoded(transport, client)
        failure = caught.exception
        self.assertEqual(failure.primary.reason, 'invalid_encoding')
        self.assertEqual(failure.response_head.headers[-1], (b'x-request-id', b'model-7'))
        self.assertIsNone(failure.network_failure)
        self.assertIs(transport.last_failure, failure)
        self.assertIn('incomplete or invalid', str(failure))

    async def test_unsupported_codec_does_not_consume_body_or_charge(self):
        client = Client([b'private'], headers=[(b'content-encoding', b'br')])
        transport = self.transport()
        charged = []
        with self.assertRaises(m.ExchangeFailed) as caught:
            await self.decoded(transport, client, account_bytes=charged.append)
        self.assertEqual(caught.exception.primary.reason, 'unsupported_encoding')
        self.assertFalse(client.stream.iterated)
        self.assertEqual(charged, [])

    async def test_actual_stream_failure_keeps_exact_network_marker(self):
        original = httpcore.ReadError('private stream failure')
        client = Client([b'hello'], failure=original)
        transport = self.transport()
        with self.assertRaises(m.ExchangeFailed) as caught:
            await self.decoded(transport, client)
        self.assertIs(caught.exception.primary, original)
        self.assertIs(caught.exception.network_failure, original)
        self.assertEqual(caught.exception.response_head.status, 200)
        self.assertTrue(transport.retirement_confirmed)

    async def test_account_failure_keeps_exact_local_cause(self):
        original = RuntimeError('local accounting failure')
        client = Client([b'hello'])
        transport = self.transport()
        with self.assertRaises(m.ExchangeFailed) as caught:
            await self.decoded(transport, client, account_bytes=lambda size: (_ for _ in ()).throw(original))
        self.assertIs(caught.exception.primary, original)
        self.assertIsNone(caught.exception.network_failure)

    async def test_decode_and_independent_close_failure_both_survive(self):
        original = OSError('private close failure')
        client = Client([b'bad'], headers=[(b'content-encoding', b'gzip')], close_failure=original)
        transport = self.transport()
        with self.assertRaises(m.ExchangeFailed) as caught:
            await self.decoded(transport, client)
        self.assertIs(type(caught.exception.primary), DecodedBodyRejected)
        self.assertEqual(caught.exception.cleanup_failures, (original,))
        self.assertFalse(transport.retirement_confirmed)
        self.assertIn('retirement is unconfirmed', str(caught.exception))
        with self.assertRaises(m.ExchangeFailed):
            await transport.exchange(self.request(), maximum_bytes=0)

    async def test_cancel_observes_same_cleanup_task_and_original_owner(self):
        gate = asyncio.Event()
        client = Client([b'body'], close_gate=gate)
        transport = self.transport()
        owner = object()
        with patch.object(m.httpcore, 'AsyncConnectionPool', return_value=client):
            caller = asyncio.create_task(transport.exchange_decoded(self.request(), maximum_bytes=1024, error_maximum_bytes=128, owner=owner))
            await client.close_started.wait()
            original_task = client.task
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertFalse(original_task.done())
            self.assertIs(transport.pending_owner, owner)
            self.assertFalse(transport.retirement_confirmed)
            with self.assertRaises(m.ExchangeFailed):
                await transport.retire(owner=object())
            gate.set()
            await transport.retire(owner=owner)
            self.assertTrue(original_task.done())
            self.assertTrue(transport.retirement_confirmed)

    async def test_decoder_process_control_failure_retains_original_owner_and_cleanup(self):
        for fatal in (KeyboardInterrupt(), SystemExit(0)):
            with self.subTest(kind=type(fatal)):
                cleanup = OSError('original client close failed')
                preceding = RuntimeError('prior protected evidence')
                fatal.__cause__ = preceding
                client = Client([b'x'], close_failure=cleanup)
                decoder = unittest.mock.Mock()
                decoder.feed.side_effect = fatal
                transport = self.transport()
                owner = object()
                with patch.object(m, 'DecodedBody', return_value=decoder):
                    with self.assertRaises(type(fatal)) as caught:
                        await self.decoded(transport, client, owner=owner)
                self.assertIs(caught.exception, fatal)
                self.assertIs(fatal.__cause__, preceding)
                self.assertIs(transport.last_failure.primary, fatal)
                self.assertEqual(transport.last_failure.cleanup_failures, (cleanup,))
                self.assertEqual(transport.last_failure.response_head.status, 200)
                self.assertIs(transport.pending_owner, owner)
                self.assertFalse(transport.retirement_confirmed)

    async def test_raw_and_file_modes_keep_encoded_bytes_and_zero_limit(self):
        wire = gzip.compress(b'body')
        transport = self.transport()
        for file_mode in (False, True):
            client = Client([wire], headers=[(b'content-encoding', b'gzip')])
            with patch.object(m.httpcore, 'AsyncConnectionPool', return_value=client), tempfile.TemporaryFile() as output:
                if file_mode:
                    response = await transport.exchange_file(self.request(), output_fd=output.fileno(), maximum_bytes=len(wire))
                    output.seek(0)
                    self.assertEqual(output.read(), wire)
                    self.assertEqual(response.byte_length, len(wire))
                    os.write(output.fileno(), b'still borrowed')
                else:
                    self.assertEqual((await transport.exchange(self.request(), maximum_bytes=len(wire))).body, wire)
        empty = Client([])
        with patch.object(m.httpcore, 'AsyncConnectionPool', return_value=empty):
            self.assertEqual((await transport.exchange(self.request(), maximum_bytes=0)).body, b'')

    async def test_decoded_configuration_rejected_before_pool_or_dispatch(self):
        transport = self.transport()
        bad = (0, -1, True, 1.0, '5', 2**100)
        with patch.object(m.httpcore, 'AsyncConnectionPool', side_effect=AssertionError('I/O dispatched')):
            for value in bad:
                for field in ('maximum_bytes', 'error_maximum_bytes'):
                    kwargs = dict(maximum_bytes=10, error_maximum_bytes=10)
                    kwargs[field] = value
                    with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                        await transport.exchange_decoded(self.request(), **kwargs)
            for status in (True, 99, 600, '200', 200.0):
                with self.subTest(status=status), self.assertRaises(ValueError):
                    await transport.exchange_decoded(self.request(), maximum_bytes=10, error_maximum_bytes=10, success_status=status)
        self.assertTrue(transport.retirement_confirmed)

    async def test_http_requires_explicit_direct_profile_and_tls_verification_remains(self):
        with patch.object(m.httpcore, 'AsyncConnectionPool', side_effect=AssertionError('I/O dispatched')):
            with self.assertRaises(ValueError):
                await self.transport().exchange_decoded(self.request('http://model.example/chat'), maximum_bytes=10, error_maximum_bytes=10)
        for value in (0, 1, None, 'yes'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.transport(allow_http=value)
        with self.assertRaises(ValueError):
            self.transport(allow_http=True, proxy_socket='/tmp/proxy.sock')
        trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        trust.check_hostname = False
        trust.verify_mode = ssl.CERT_NONE
        with self.assertRaises(ValueError):
            m.HttpTransport(trust=trust, exchange_seconds=1, retirement_seconds=1, allow_http=True)

    async def test_actual_direct_http_uses_same_transport_and_complete_decoding(self):
        wire = gzip.compress(b'actual HTTP loopback')
        async def serve(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: '+str(len(wire)).encode()+b'\r\nConnection: close\r\n\r\n'+wire)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            transport = self.transport(allow_http=True)
            response = await transport.exchange_decoded(httpx.Request('GET', f'http://127.0.0.1:{port}/'), maximum_bytes=128, error_maximum_bytes=64)
        self.assertEqual(response.body, b'actual HTTP loopback')
        self.assertTrue(transport.retirement_confirmed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
