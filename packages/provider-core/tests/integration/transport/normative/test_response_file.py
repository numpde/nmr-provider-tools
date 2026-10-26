"""Real TLS response bytes reach disk before the response can finish."""

import asyncio
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from provider_core.transport import HttpTransport
from tests.fakes.tls_peer import TlsPeer


class FileResponsePeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_tls_response_yields_exact_file_transfer_facts(self):
        body = b"bounded TLS body\n" * 20000
        with tempfile.TemporaryFile() as output:
            async with TlsPeer("success", response_body=body) as peer:
                transport = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=0.5)
                response = await transport.exchange_file(
                    httpx.Request("GET", peer.origin + "/input"), output_fd=output.fileno(), maximum_bytes=len(body)
                )
                self.assertEqual(response.byte_length, len(body))
                self.assertEqual(response.content_hash, "sha256:" + hashlib.sha256(body).hexdigest())
                self.assertEqual(response.status, 200)
                output.seek(0)
                self.assertEqual(output.read(), body)
                await asyncio.wait_for(peer.closed.wait(), 1)
                self.assertTrue(transport.retirement_confirmed)
                self.assertEqual(len(peer.requests), 1)

    async def test_stalled_tls_body_is_already_on_disk_before_cancellation(self):
        written = asyncio.Event()
        loop = asyncio.get_running_loop()
        write = os.write

        def observe_write(descriptor, data):
            count = write(descriptor, data)
            loop.call_soon_threadsafe(written.set)
            return count

        with tempfile.TemporaryFile() as output:
            async with TlsPeer("body-stall") as peer:
                transport = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=0.5)
                with patch("provider_core.transport.os.write", side_effect=observe_write):
                    task = asyncio.create_task(
                        transport.exchange_file(
                            httpx.Request("GET", peer.origin + "/input"), output_fd=output.fileno(), maximum_bytes=100
                        )
                    )
                    try:
                        await asyncio.wait_for(written.wait(), 1)
                        self.assertFalse(task.done())
                        self.assertEqual(os.pread(output.fileno(), 1, 0), b"x")
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                await asyncio.wait_for(peer.closed.wait(), 1)
                self.assertTrue(transport.retirement_confirmed)
                self.assertEqual(len(peer.requests), 1)
