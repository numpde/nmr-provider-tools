"""Shrink complete-body and truncation boundaries using real TLS and CONNECT."""

import asyncio
import unittest

from hypothesis import given, settings, strategies as st
import httpx

from tests.fakes.tls_shutdown import ShutdownPeer, fixed_response


class GeneratedShutdownTests(unittest.TestCase):
    @settings(max_examples=24, deadline=None, database=None)
    @given(body=st.binary(max_size=256), tunnel=st.booleans(), fragment_size=st.integers(1, 31))
    def test_complete_content_length_preserves_generated_octets(self, body, tunnel, fragment_size):
        async def probe():
            async with ShutdownPeer([fixed_response(body)], tunnel=tunnel, fragment_size=fragment_size) as peer:
                response = await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=256)
                self.assertEqual(body, response.body)
                self.assertTrue(peer.transport.retirement_confirmed)
                self.assertGreater(peer.raw_close_successes, 0)
                self.assertTrue(all(sock.fileno() == -1 for sock in peer.raw_sockets))
        asyncio.run(probe())

    @settings(max_examples=24, deadline=None, database=None)
    @given(chunks=st.lists(st.binary(min_size=1, max_size=32), max_size=6), tunnel=st.booleans(), fragment_size=st.integers(1, 31))
    def test_terminal_chunk_preserves_generated_octets(self, chunks, tunnel, fragment_size):
        body = b"".join(chunks)
        framed = b"".join(format(len(chunk), "x").encode() + b"\r\n" + chunk + b"\r\n" for chunk in chunks)
        wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + framed + b"0\r\n\r\n"

        async def probe():
            async with ShutdownPeer([wire], tunnel=tunnel, fragment_size=fragment_size) as peer:
                response = await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=256)
                self.assertEqual(body, response.body)
                self.assertTrue(peer.transport.retirement_confirmed)
                self.assertGreater(peer.raw_close_successes, 0)
                self.assertTrue(all(sock.fileno() == -1 for sock in peer.raw_sockets))
        asyncio.run(probe())

