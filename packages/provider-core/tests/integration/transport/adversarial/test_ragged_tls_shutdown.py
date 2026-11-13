"""Missing TLS goodbye cannot authorize partial, malformed or oversized replies."""

import asyncio
import gzip
import ssl
import unittest

import anyio
import httpcore
import httpx

from provider_core.decoded_body import DecodedBodyRejected
from provider_core.transport import ExchangeFailed
from tests.fakes.tls_shutdown import ShutdownPeer, fixed_response


class RejectedResponseShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def assert_transient_close_failure(self, failure):
        for tunnel in (False, True):
            with self.subTest(tunnel=tunnel):
                async with ShutdownPeer([fixed_response(b"data")], tunnel=tunnel, raw_close_failures=(failure,)) as peer:
                    with self.assertRaises(ExchangeFailed) as caught:
                        await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
                    self.assertIn(failure, caught.exception.cleanup_failures)
                    self.assertGreaterEqual(peer.raw_close_attempts, 2)
                    self.assertGreater(peer.raw_close_successes, 0)
                    self.assertTrue(all(sock.fileno() == -1 for sock in peer.raw_sockets))

    async def test_transient_raw_eof_remains_visible_after_successful_retry(self):
        await self.assert_transient_close_failure(ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "synthetic first close EOF"))

    async def test_transient_wrapped_raw_eof_remains_visible_after_retry(self):
        failure = anyio.BrokenResourceError("synthetic first close failed")
        failure.__cause__ = ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "synthetic first close EOF")
        await self.assert_transient_close_failure(failure)

    async def test_cancelled_pending_raw_close_keeps_original_owner(self):
        async with ShutdownPeer([fixed_response(b"data")], tunnel=True, hold_raw_close=True) as peer:
            owner = object()
            task = asyncio.create_task(peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4, owner=owner))
            try:
                await asyncio.wait_for(peer.raw_close_started.wait(), 2)
                task.cancel("cancel pending raw close")
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task, 2)
                self.assertEqual(("cancel pending raw close",), caught.exception.args)
                self.assertFalse(peer.transport.retirement_confirmed)
                self.assertIs(owner, peer.transport.pending_owner)
                self.assertEqual(0, peer.raw_close_successes)
            finally:
                peer.raw_close_release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_close_deadline_cannot_authorize_replacement(self):
        async with ShutdownPeer([fixed_response(b"data")], tunnel=False, hold_raw_close=True) as peer:
            owner = object()
            with self.assertRaises(ExchangeFailed):
                await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4, owner=owner)
            self.assertFalse(peer.transport.retirement_confirmed)
            self.assertIs(owner, peer.transport.pending_owner)
            with self.assertRaises(ExchangeFailed):
                await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4, owner=object())
            self.assertEqual(1, len(peer.requests))

    async def test_wrong_owner_cannot_retire_pending_raw_close(self):
        async with ShutdownPeer([fixed_response(b"data")], tunnel=True, hold_raw_close=True) as peer:
            owner = object()
            task = asyncio.create_task(peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4, owner=owner))
            try:
                await asyncio.wait_for(peer.raw_close_started.wait(), 2)
                with self.assertRaises(ExchangeFailed):
                    await peer.transport.retire(owner=object())
                self.assertIs(owner, peer.transport.pending_owner)
                self.assertFalse(peer.transport.retirement_confirmed)
                self.assertEqual(0, peer.raw_close_successes)
            finally:
                peer.raw_close_release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def assert_close_failure(self, failure):
        for tunnel in (False, True):
            with self.subTest(tunnel=tunnel):
                async with ShutdownPeer([fixed_response(b"data")], tunnel=tunnel, raw_close_failure=failure) as peer:
                    with self.assertRaises(ExchangeFailed) as caught:
                        await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
                    self.assertIn(failure, caught.exception.cleanup_failures)
                    self.assertGreater(peer.raw_close_attempts, 0)
                    self.assertEqual(0, peer.raw_close_successes)
                    self.assertFalse(peer.transport.retirement_confirmed)
                    with self.assertRaises(ExchangeFailed):
                        await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)

    async def test_raw_socket_close_error_is_not_a_harmless_tls_goodbye(self):
        await self.assert_close_failure(OSError("synthetic raw close failed"))

    async def test_raw_close_eof_error_is_not_peer_goodbye(self):
        await self.assert_close_failure(ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "synthetic raw close EOF"))

    async def test_wrapped_raw_close_eof_still_requires_close_proof(self):
        failure = anyio.BrokenResourceError("synthetic raw close failed")
        failure.__cause__ = ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "synthetic raw close EOF")
        await self.assert_close_failure(failure)

    async def test_unrelated_ssl_error_during_raw_close_is_not_ignored(self):
        await self.assert_close_failure(ssl.SSLError(ssl.SSL_ERROR_SSL, "synthetic raw close TLS failure"))

    async def test_unrelated_broken_resource_during_raw_close_is_not_ignored(self):
        await self.assert_close_failure(anyio.BrokenResourceError("synthetic raw resource close failed"))

    # silent-defaults: allow-contract -- Ordinary negative probes allow 4 KiB of raw response and assert any original failure; specific cases tighten the bound or failure type.
    async def assert_rejected(self, wire, *, maximum_bytes=4096, decoded=False, primary=None):
        for tunnel in (False, True):
            with self.subTest(tunnel=tunnel):
                async with ShutdownPeer([wire], tunnel=tunnel) as peer:
                    request = httpx.Request("GET", peer.origin)
                    with self.assertRaises(ExchangeFailed) as caught:
                        if decoded:
                            await peer.transport.exchange_decoded(request, maximum_bytes=maximum_bytes, error_maximum_bytes=4096)
                        else:
                            await peer.transport.exchange(request, maximum_bytes=maximum_bytes)
                    self.assertIsNotNone(caught.exception.primary)
                    if primary is not None:
                        self.assertIsInstance(caught.exception.primary, primary)
                    self.assertTrue(peer.transport.retirement_confirmed)
                self.assertEqual(1, len(peer.requests))

    async def test_content_length_body_is_short(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\ndata")

    async def test_content_length_body_is_entirely_absent(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n")

    async def test_chunk_payload_is_short(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\ndata")

    async def test_chunked_body_has_no_terminal_chunk(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n")

    async def test_chunk_trailers_are_unterminated(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX-Probe: missing-end\r\n")

    async def test_chunk_size_is_malformed(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZZ\r\n", primary=httpcore.RemoteProtocolError)

    async def test_chunk_payload_terminator_is_malformed(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nxXX", primary=httpcore.RemoteProtocolError)

    async def test_conflicting_content_lengths(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nx", primary=httpcore.RemoteProtocolError)

    async def test_non_numeric_content_length(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nContent-Length: nope\r\n\r\n", primary=httpcore.RemoteProtocolError)

    async def test_malformed_header_precedes_unread_entity(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nbad header: x\r\nContent-Length: 4\r\n\r\ndata", primary=httpcore.RemoteProtocolError)

    async def test_close_delimited_entity_requires_authenticated_eof(self):
        await self.assert_rejected(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nambiguous")

    async def test_completed_entity_exceeds_byte_limit(self):
        await self.assert_rejected(fixed_response(b"12345"), maximum_bytes=4)

    async def test_complete_http_framing_does_not_complete_truncated_gzip(self):
        encoded = gzip.compress(b"decoded")[:-4]
        await self.assert_rejected(fixed_response(encoded, headers=b"Content-Encoding: gzip\r\n"), decoded=True, primary=DecodedBodyRejected)

    async def test_peer_that_never_finishes_shutdown_remains_deadline_bounded(self):
        for tunnel in (False, True):
            with self.subTest(tunnel=tunnel):
                async with ShutdownPeer([fixed_response(b"data")], tunnel=tunnel, hold=True) as peer:
                    with self.assertRaises(ExchangeFailed) as caught:
                        await peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4)
                    self.assertIsInstance(caught.exception.primary, TimeoutError)
                    self.assertTrue(peer.transport.retirement_confirmed)

    async def test_cancellation_during_shutdown_preserves_caller_cancellation(self):
        for tunnel in (False, True):
            with self.subTest(tunnel=tunnel):
                async with ShutdownPeer([fixed_response(b"data")], tunnel=tunnel, hold=True) as peer:
                    task = asyncio.create_task(peer.transport.exchange(httpx.Request("GET", peer.origin), maximum_bytes=4))
                    try:
                        await asyncio.wait_for(peer.shutdown_started.wait(), 2)
                        task.cancel("caller stopped shutdown probe")
                        with self.assertRaises(asyncio.CancelledError) as caught:
                            await task
                        self.assertEqual(("caller stopped shutdown probe",), caught.exception.args)
                        self.assertTrue(peer.transport.retirement_confirmed)
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
