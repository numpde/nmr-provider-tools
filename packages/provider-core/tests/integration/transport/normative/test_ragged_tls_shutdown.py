"""Complete HTTP framing survives a missing TLS goodbye after socket retirement."""

import gzip
import tempfile
import unittest

import httpx

from tests.fakes.tls_shutdown import ShutdownPeer, fixed_response


class CompleteResponseShutdownTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- Ordinary probes read a raw GET response; separate scenarios select decoding, file output, HEAD or fragmented writes.
    async def assert_complete(self, wire, expected, *, method="GET", decoded=False, fragment_size=None, file=False):
        for tunnel in (False, True):
            with self.subTest(tunnel=tunnel):
                async with ShutdownPeer([wire], tunnel=tunnel, fragment_size=fragment_size) as peer:
                    request = httpx.Request(method, peer.origin)
                    options = {"maximum_bytes": 4096}
                    if decoded:
                        response = await peer.transport.exchange_decoded(request, error_maximum_bytes=4096, **options)
                        self.assertEqual(expected, response.body)
                    elif file:
                        with tempfile.TemporaryFile() as output:
                            response = await peer.transport.exchange_file(request, output_fd=output.fileno(), **options)
                            output.seek(0)
                            self.assertEqual(expected, output.read())
                            self.assertEqual(len(expected), response.byte_length)
                    else:
                        response = await peer.transport.exchange(request, **options)
                        self.assertEqual(expected, response.body)
                    self.assertTrue(peer.transport.retirement_confirmed)
                    self.assertIsNone(peer.transport.pending_owner)
                    self.assertGreater(peer.raw_close_successes, 0)
                    self.assertTrue(all(sock.fileno() == -1 for sock in peer.raw_sockets))
                self.assertEqual(1, peer.connections_closed)
                self.assertEqual(1, len(peer.requests))

    async def test_exact_content_length_body(self):
        await self.assert_complete(fixed_response(b"data"), b"data")

    async def test_zero_length_entity(self):
        await self.assert_complete(fixed_response(b""), b"")

    async def test_binary_entity_preserves_all_octets(self):
        body = bytes(range(256))
        await self.assert_complete(fixed_response(body), body)

    async def test_fragmented_headers_and_body(self):
        await self.assert_complete(fixed_response(b"fragmented"), b"fragmented", fragment_size=1)

    async def test_chunked_entity_with_terminal_chunk(self):
        await self.assert_complete(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n0\r\n\r\n", b"data")

    async def test_chunk_extensions_do_not_change_entity(self):
        await self.assert_complete(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4;key=value\r\ndata\r\n0\r\n\r\n", b"data")

    async def test_chunk_trailers_are_consumed_before_completion(self):
        await self.assert_complete(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n0\r\nX-Probe: done\r\n\r\n", b"data")

    async def test_empty_chunked_entity(self):
        await self.assert_complete(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", b"")

    async def test_head_has_no_entity_despite_content_length(self):
        await self.assert_complete(b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n", b"", method="HEAD")

    async def test_204_has_no_entity(self):
        await self.assert_complete(b"HTTP/1.1 204 No Content\r\n\r\n", b"")

    async def test_304_has_no_entity_despite_content_length(self):
        await self.assert_complete(b"HTTP/1.1 304 Not Modified\r\nContent-Length: 999\r\n\r\n", b"")

    async def test_http_rejection_remains_a_completed_response(self):
        await self.assert_complete(fixed_response(b"denied", status=b"401 Unauthorized"), b"denied")

    async def test_connection_close_header_with_framed_entity(self):
        await self.assert_complete(fixed_response(b"data", headers=b"Connection: close\r\n"), b"data")

    async def test_complete_gzip_is_decoded(self):
        await self.assert_complete(fixed_response(gzip.compress(b"decoded"), headers=b"Content-Encoding: gzip\r\n"), b"decoded", decoded=True)

    async def test_file_response_survives_ragged_shutdown(self):
        await self.assert_complete(fixed_response(b"file data"), b"file data", file=True)

    async def test_retired_transport_can_execute_a_second_exchange(self):
        for tunnel in (False, True):
            with self.subTest(tunnel=tunnel):
                async with ShutdownPeer([fixed_response(b"first"), fixed_response(b"second")], tunnel=tunnel) as peer:
                    for expected in (b"first", b"second"):
                        response = await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=32)
                        self.assertEqual(expected, response.body)
                        self.assertTrue(peer.transport.retirement_confirmed)
                self.assertEqual(2, len(peer.requests))
