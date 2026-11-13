"""Generated truncation points cannot borrow success from complete HTTP headers."""

import asyncio
import gzip
import unittest

from hypothesis import given, settings, strategies as st
import httpx

from provider_core.transport import ExchangeFailed
from tests.fakes.tls_shutdown import ShutdownPeer, fixed_response


class GeneratedTruncationTests(unittest.TestCase):
    async def assert_rejected(self, wire, tunnel, *, decoded):
        async with ShutdownPeer([wire], tunnel=tunnel) as peer:
            with self.assertRaises(ExchangeFailed) as caught:
                request = httpx.Request("GET", peer.origin)
                if decoded:
                    await peer.transport.exchange_decoded(request, maximum_bytes=512, error_maximum_bytes=512)
                else:
                    await peer.transport.exchange(request, maximum_bytes=512)
            self.assertIsNotNone(caught.exception.primary)
            self.assertTrue(peer.transport.retirement_confirmed)
            self.assertGreater(peer.raw_close_successes, 0)
            self.assertTrue(all(sock.fileno() == -1 for sock in peer.raw_sockets))
            self.assertEqual(1, len(peer.requests))

    @settings(max_examples=32, deadline=None, database=None)
    @given(body=st.binary(min_size=1, max_size=256), tunnel=st.booleans(), data=st.data())
    def test_generated_content_length_truncation_is_rejected(self, body, tunnel, data):
        cut = data.draw(st.integers(0, len(body) - 1), label="received entity bytes")
        wire = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body[:cut]
        asyncio.run(self.assert_rejected(wire, tunnel, decoded=False))

    @settings(max_examples=32, deadline=None, database=None)
    @given(body=st.binary(min_size=1, max_size=128), tunnel=st.booleans(), data=st.data())
    def test_generated_chunked_truncation_is_rejected(self, body, tunnel, data):
        framed = format(len(body), "x").encode() + b"\r\n" + body + b"\r\n0\r\n\r\n"
        cut = data.draw(st.integers(0, len(framed) - 1), label="received chunk framing bytes")
        wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + framed[:cut]
        asyncio.run(self.assert_rejected(wire, tunnel, decoded=False))

    @settings(max_examples=24, deadline=None, database=None)
    @given(body=st.binary(min_size=1, max_size=128), tunnel=st.booleans(), missing=st.integers(1, 8))
    def test_truncated_gzip_with_complete_http_framing_is_rejected(self, body, tunnel, missing):
        encoded = gzip.compress(body)[:-missing]
        wire = fixed_response(encoded, headers=b"Content-Encoding: gzip\r\n")
        asyncio.run(self.assert_rejected(wire, tunnel, decoded=True))
