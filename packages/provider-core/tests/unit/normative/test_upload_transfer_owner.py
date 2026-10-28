"""Independent source policies reuse the same verified transfer owner."""

import asyncio
import os
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from provider_core.uploads import UploadFailed, UploadTransfer, VerifiedUploadFile
from tests.fakes.acquisition import file_sources, private_source_directory, sources
from tests.fakes.transport import Client, Stream
from tests.fakes.acquisition import store_headers


class UploadTransferOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_grant_factory_respects_transfer_deadline_and_cleans_file(self):
        _, _, store, _ = file_sources()
        deadline = asyncio.get_running_loop().time() + 0.1
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: deadline - asyncio.get_running_loop().time(),
                                  before_request=lambda: None, account=lambda _count: None)
        original = RuntimeError("grant owner retained an interrupted exchange")

        async def grant():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                transfer.last_failure = original
                raise

        with private_source_directory() as (root, directory):
            with self.assertRaisesRegex(UploadFailed, "deadline expired"):
                await asyncio.wait_for(transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1), 1)
            self.assertIs(transfer.last_failure.cause, original)
            self.assertIsInstance(transfer.last_failure.interruption, TimeoutError)
            self.assertFalse(transfer.last_failure.grant_issued)
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_buffered_download_rejects_malformed_grant_before_sending_bearer(self):
        owner, api, store = sources()
        api.grant = replace(api.grant, capability="x" * 2049)
        await owner.inspect()
        with self.assertRaises(UploadFailed):
            await owner.acquire(api.member.upload_ref)
        self.assertEqual(store.calls, [])

    async def test_forged_grant_is_rejected_before_sending_bearer(self):
        _, api, store, client = file_sources()
        for change in ({"upload_ref": "bad?query"}, {"content_hash": "bad"},
                       {"byte_length": True}, {"expires_at": datetime(2099, 1, 1)},
                       {"capability": "x" * 2049}):
            with self.subTest(change=change), private_source_directory() as (root, directory), patch(
                "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
            ):
                async def grant():
                    return replace(api.grant, **change)
                transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                          remaining=lambda: 1, before_request=lambda: None, account=lambda _count: None)
                with self.assertRaises(UploadFailed):
                    await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
                self.assertEqual(client.calls, [])
                self.assertEqual(list(Path(root).iterdir()), [])

    async def test_direct_request_budget_failure_stops_before_store_and_removes_file(self):
        _, api, store, client = file_sources()
        original = RuntimeError("request budget refused")
        async def grant():
            return api.grant
        def before_request():
            raise original
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: 1, before_request=before_request, account=lambda _count: None)
        with private_source_directory() as (root, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
        ):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
            self.assertIs(caught.exception.cause, original)
            self.assertEqual(client.calls, [])
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_direct_store_exchange_is_bounded_by_remaining_deadline(self):
        _, api, store, _ = file_sources()
        blocked = asyncio.Event()
        client = Client(Stream([b"geometry"], stall=blocked), headers=store_headers(b"geometry"))
        deadline = asyncio.get_running_loop().time() + 0.1
        async def grant():
            return api.grant
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: deadline - asyncio.get_running_loop().time(),
                                  before_request=lambda: None, account=lambda _count: None)
        with private_source_directory() as (root, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
        ):
            with self.assertRaisesRegex(UploadFailed, "deadline expired") as caught:
                await asyncio.wait_for(transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1), 2)
            self.assertIsNotNone(caught.exception.cause)
            self.assertFalse(caught.exception.grant_issued)
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_direct_accounting_failure_preserves_cause_and_cleans_file(self):
        _, api, store, client = file_sources()
        original = RuntimeError("accounting refused")
        async def grant():
            return api.grant
        def account(_count):
            raise original
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: 1, before_request=lambda: None, account=account)
        with private_source_directory() as (root, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
        ):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
            self.assertIs(caught.exception.cause.primary, original)
            self.assertFalse(caught.exception.grant_issued)
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_direct_pending_store_write_retains_original_owner_until_retirement(self):
        _, api, store, client = file_sources()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        write = os.write
        async def grant():
            return api.grant
        def held_write(descriptor, data):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("test did not release the original write")
            return write(descriptor, data)
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: 1, before_request=lambda: None, account=lambda _count: None)
        with private_source_directory() as (root, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
        ), patch("provider_core.transport.os.write", side_effect=held_write):
            task = asyncio.create_task(transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                self.assertIs(caught.exception.__cause__, transfer.last_failure)
                self.assertIs(store.pending_owner, transfer)
                with self.assertRaises(UploadFailed):
                    await transfer.settle_file()
                self.assertEqual(len(list(Path(root).iterdir())), 1)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await store.retire(owner=transfer)
                await transfer.settle_file()
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_grant_factory_failure_preserves_cause_and_removes_opened_file(self):
        _, api, store, _ = file_sources()
        original = RuntimeError("grant callback failed")
        async def grant():
            raise original
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: 1, before_request=lambda: None, account=lambda _count: None)
        with private_source_directory() as (root, directory):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
            self.assertIs(caught.exception.cause, original)
            self.assertFalse(caught.exception.grant_issued)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(api.calls, [])

    async def test_direct_owner_creates_before_grant_and_verifies_without_membership_reads(self):
        _, api, store, client = file_sources()
        charged = []


        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: 1, before_request=lambda: None, account=charged.append)
        with private_source_directory() as (root, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
        ):
            async def grant():
                self.assertEqual(len(list(Path(root).iterdir())), 1)
                return api.grant
            receipt = await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
            self.assertIs(type(receipt), VerifiedUploadFile)
            self.assertEqual(Path(root, receipt.name).read_bytes(), b"geometry")
            self.assertEqual(charged, [8])
            self.assertEqual(api.calls, [])
            self.assertTrue(store.retirement_confirmed)
            os.unlink(receipt.name, dir_fd=directory)

    async def test_direct_corruption_does_not_invent_issuance_and_removes_partial_file(self):
        _, api, store, _ = file_sources()
        client = Client(Stream([b"badbytes"]), headers=store_headers(b"geometry"))
        async def grant():
            return api.grant
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda: 1, before_request=lambda: None, account=lambda _count: None)
        with private_source_directory() as (root, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
        ):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
            self.assertFalse(caught.exception.grant_issued)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(api.calls, [])
