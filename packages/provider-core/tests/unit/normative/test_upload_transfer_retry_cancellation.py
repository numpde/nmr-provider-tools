"""Cancellation during private-file abort cannot revive transfer permission."""
import asyncio
import os
import threading
import unittest
from unittest.mock import patch

from provider_core.uploads import UploadTransfer
from tests.fakes.acquisition import file_sources, private_source_directory, store_headers
from tests.fakes.transport import Client, Stream
from tests.unit.normative.test_upload_transfer_retry import temporary_read_error

class UploadTransferCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_abort_cancellation_remains_ineligible_after_settlement(self):
        _, api, store, _ = file_sources()
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: 2, before_request=lambda: None, account=lambda _: None)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        close = os.close
        def held_close(fd):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise TimeoutError("probe failed to release close")
            return close(fd)
        async def grant():
            return api.grant
        with private_source_directory() as (_, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool",
            return_value=Client(Stream([], failure=temporary_read_error()), headers=store_headers(b"geometry")),
        ), patch("provider_core.uploads.os.close", side_effect=held_close):
            task = asyncio.create_task(transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.3))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                failure = transfer.last_failure
                self.assertIs(caught.exception.__cause__, failure)
                self.assertTrue(store.retirement_confirmed)
                self.assertIsNone(failure.interruption)
                self.assertIsNone(failure.cause.interruption)
                release.set()
                await transfer.settle_file()
                self.assertFalse(transfer.can_retry_transfer(failure))
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                if transfer.file_cleanup_pending:
                    await transfer.settle_file()
