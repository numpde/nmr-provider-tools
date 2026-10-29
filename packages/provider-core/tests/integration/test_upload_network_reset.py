"""One real TLS reset and the same ReadError raised by local accounting."""

import asyncio
import socket
import struct
import unittest
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpcore

from provider_core.transport import HttpTransport, temporary_io_failure
from provider_core.upload_response import ReadGrant
from provider_core.uploads import UploadFailed, UploadTransfer
from tests.fakes.acquisition import private_source_directory, store_headers
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF


BODY = b"geometry"


class ResetPeer(TlsPeer):
    async def handle(self, reader, writer):
        self.handlers.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\n" + b"".join(name+b": "+value+b"\r\n" for name,value in store_headers(BODY))
                         + b"\r\ng")
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii",1,0))
            writer.transport.abort()
            self.closed.set()
        finally:
            writer.close()


class UploadNetworkResetTests(unittest.IsolatedAsyncioTestCase):
    async def failure(self, peer, account):
        store = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.2)
        transfer = UploadTransfer("job:example", store=store, store_origin=peer.origin, remaining=lambda:1,
                                  before_request=lambda:None, account=account)
        grant = ReadGrant("job:example", UPLOAD_REF, 8, "sha256:"+sha256(BODY).hexdigest(),
                          peer.origin+"/upload/v1/uploads/"+UPLOAD_REF+"/bytes", "protected-bearer",
                          datetime.now(UTC)+timedelta(minutes=1))
        async def fresh():
            return grant
        with private_source_directory() as (_, directory):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(fresh, directory_fd=directory, retirement_seconds=0.2)
        self.assertTrue(store.retirement_confirmed)
        self.assertFalse(transfer.file_cleanup_pending)
        await asyncio.wait_for(peer.closed.wait(),1)
        return transfer, caught.exception

    async def test_actual_read_reset_context_requires_exact_network_origin(self):
        async with ResetPeer("success") as peer:
            transfer, failure = await self.failure(peer, lambda _:None)
            original = failure.cause.primary
            self.assertIs(type(original),httpcore.ReadError)
            self.assertIs(failure.cause.network_failure,original)
            self.assertIsNone(original.__cause__)
            self.assertFalse(temporary_io_failure(original,8))
            self.assertTrue(transfer.can_retry_transfer(failure))
        def account(_count):
            raise original
        headers = [(name,value) for name,value in store_headers(BODY) if name!=b"content-length"]
        async with TlsPeer("success",response_body=BODY,response_headers=headers) as peer:
            transfer, failure = await self.failure(peer,account)
            self.assertIs(failure.cause.primary,original)
            self.assertIsNone(failure.cause.network_failure)
            self.assertFalse(transfer.can_retry_transfer(failure))
