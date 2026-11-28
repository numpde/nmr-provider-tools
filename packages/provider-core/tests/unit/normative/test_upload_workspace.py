"""Private sources outlive transfers and readers, including failed retirement."""

import asyncio
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.upload_file import UploadFailed, read_upload_files
from provider_core.upload_workspace import UploadWorkspace
from tests.fakes.acquisition import file_sources
from tests.fakes.uploads import UPLOAD_REF


class UploadWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_transfer_settles_original_file_before_directory_removal(self):
        uploads, api, _, _ = file_sources()
        api.retire = AsyncMock()
        await uploads.inspect()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        chmod = os.fchmod

        def held_chmod(descriptor, mode):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=3):
                raise TimeoutError("fixture file creation did not resume")
            chmod(descriptor, mode)

        with TemporaryDirectory() as parent:
            directory = Path(parent, "sources")
            workspace = UploadWorkspace(directory, uploads=uploads, retirement_seconds=0.02,
                                        retire_readers=AsyncMock())
            workspace.open()
            with patch("provider_core.uploads.os.fchmod", side_effect=held_chmod):
                acquisition = asyncio.create_task(workspace.acquire(UPLOAD_REF))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    acquisition.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await acquisition
                    self.assertTrue(directory.exists())
                    self.assertTrue(list(directory.iterdir()))
                finally:
                    release.set()
                    if not acquisition.done():
                        acquisition.cancel()
                    await asyncio.gather(acquisition, return_exceptions=True)
                    await workspace.retire()
            self.assertFalse(directory.exists())

    async def test_direct_acquisition_cannot_start_during_transport_retirement(self):
        uploads, api, _, _ = file_sources()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def retire_api():
            entered.set()
            await finish.wait()

        api.retire = retire_api
        retirement = asyncio.create_task(uploads.retire())
        await entered.wait()
        try:
            with self.assertRaises(UploadFailed):
                await uploads.inspect()
            with self.assertRaises(UploadFailed):
                await uploads.acquire(UPLOAD_REF)
            self.assertEqual(api.calls, [])
        finally:
            finish.set()
            await retirement

    async def test_retirement_waits_for_acquisition_and_refuses_new_work(self):
        with TemporaryDirectory() as parent:
            directory = Path(parent, "sources")
            entered, finish = asyncio.Event(), asyncio.Event()

            async def acquire(upload_ref, **kwargs):
                entered.set()
                await finish.wait()
                return SimpleNamespace(upload_ref=upload_ref)

            uploads = SimpleNamespace(acquire_file=acquire, retire=AsyncMock())
            readers = AsyncMock()
            workspace = UploadWorkspace(directory, uploads=uploads, retirement_seconds=1,
                                        retire_readers=readers)
            workspace.open()
            acquisition = asyncio.create_task(workspace.acquire(UPLOAD_REF))
            await entered.wait()
            retirement = asyncio.create_task(workspace.retire())
            await asyncio.sleep(0)
            readers.assert_not_awaited()
            self.assertTrue(directory.exists())
            finish.set()
            await acquisition
            await retirement
            readers.assert_awaited_once()
            self.assertFalse(directory.exists())
            with self.assertRaises(UploadFailed):
                await workspace.acquire(UPLOAD_REF)

    async def test_transport_retirement_failure_retains_workspace(self):
        uploads, api, store, _ = file_sources()
        api.retire = AsyncMock()
        with TemporaryDirectory() as parent:
            directory = Path(parent, "sources")
            workspace = UploadWorkspace(directory, uploads=uploads, retirement_seconds=1,
                                        retire_readers=AsyncMock())
            workspace.open()
            with patch.object(store, "retire", new=AsyncMock(side_effect=RuntimeError("I/O remains active"))):
                with self.assertRaises(BaseExceptionGroup):
                    await workspace.retire()
                self.assertTrue(directory.exists())
            await workspace.retire()
            self.assertFalse(directory.exists())

    async def test_real_acquisition_stays_readable_until_readers_retire(self):
        uploads, api, store, client = file_sources()
        api.retire = AsyncMock()
        await uploads.inspect()
        with TemporaryDirectory() as parent:
            directory = Path(parent, "sources")

            async def retire_readers():
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    snapshots = read_upload_files(
                        dict(workspace.receipts), job_ref=uploads.job_ref,
                        directory_fd=descriptor, maximum_bytes=100, maximum_total_bytes=100,
                    )
                    self.assertEqual(snapshots[UPLOAD_REF].data, b"geometry")
                finally:
                    os.close(descriptor)

            workspace = UploadWorkspace(directory, uploads=uploads, retirement_seconds=1,
                                        retire_readers=retire_readers)
            workspace.open()
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            with patch("provider_core.adapters.httpcore.httpcore.AsyncConnectionPool", return_value=client):
                receipt = await workspace.acquire(UPLOAD_REF)
            self.assertEqual(uploads.received_bytes, 8)
            self.assertIs(workspace.receipts[UPLOAD_REF], receipt)
            await workspace.retire()
            self.assertFalse(directory.exists())
            self.assertTrue(store.retirement_confirmed)
            api.retire.assert_awaited_once()
            await workspace.retire()
            with self.assertRaises(UploadFailed):
                await workspace.acquire(UPLOAD_REF)

    async def test_failed_reader_retirement_retains_sources_and_can_retry(self):
        with TemporaryDirectory() as parent:
            directory = Path(parent, "sources")
            readers = AsyncMock(side_effect=[RuntimeError("reader still running"), None])
            uploads = SimpleNamespace(retire=AsyncMock())
            workspace = UploadWorkspace(directory, uploads=uploads, retirement_seconds=1,
                                        retire_readers=readers)
            workspace.open()
            Path(directory, "retained").write_bytes(b"input")
            with self.assertRaises(BaseExceptionGroup):
                await workspace.retire()
            self.assertEqual(Path(directory, "retained").read_bytes(), b"input")
            uploads.retire.assert_awaited_once()
            await workspace.retire()
            self.assertFalse(directory.exists())

    async def test_cancelled_reader_join_retains_workspace(self):
        with TemporaryDirectory() as parent:
            directory = Path(parent, "sources")
            readers = AsyncMock(side_effect=asyncio.CancelledError())
            workspace = UploadWorkspace(directory, uploads=SimpleNamespace(retire=AsyncMock()),
                                        retirement_seconds=1, retire_readers=readers)
            workspace.open()
            with self.assertRaises(asyncio.CancelledError):
                await workspace.retire()
            self.assertTrue(directory.exists())
            readers.side_effect = None
            await workspace.retire()

    async def test_existing_workspace_is_never_removed(self):
        with TemporaryDirectory() as parent:
            directory = Path(parent, "sources")
            directory.mkdir()
            Path(directory, "retained").write_bytes(b"input")
            workspace = UploadWorkspace(directory, uploads=SimpleNamespace(retire=AsyncMock()),
                                        retirement_seconds=1, retire_readers=AsyncMock())
            with self.assertRaises(FileExistsError):
                workspace.open()
            await workspace.retire()
            self.assertEqual(Path(directory, "retained").read_bytes(), b"input")

    async def test_uncertain_close_is_not_retried_or_deleted(self):
        with TemporaryDirectory() as parent:
            directory = Path(parent, "sources")
            workspace = UploadWorkspace(directory, uploads=SimpleNamespace(retire=AsyncMock()),
                                        retirement_seconds=1, retire_readers=AsyncMock())
            workspace.open()
            close = os.close

            def uncertain_close(descriptor):
                close(descriptor)
                raise OSError("closure not confirmed")

            with patch("provider_core.upload_workspace.os.close", side_effect=uncertain_close) as closed:
                with self.assertRaises(UploadFailed):
                    await workspace.retire()
                with self.assertRaises(BaseExceptionGroup):
                    await workspace.retire()
                self.assertEqual(closed.call_count, 1)
            self.assertTrue(directory.exists())
