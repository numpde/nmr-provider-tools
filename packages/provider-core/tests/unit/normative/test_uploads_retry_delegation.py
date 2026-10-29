"""Both acquisition paths share current failure provenance without retry dispatch."""

import asyncio
import ssl
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.transport import ExchangeFailed, HttpTransport
from provider_core.uploads import UploadFailed, UploadInputRejected, UploadTransfer
from tests.fakes.acquisition import file_sources, private_source_directory, store_headers
from tests.fakes.transport import Client, Stream
from tests.unit.normative.test_upload_transfer_retry import temporary_read_error


class JobUploadsRetryDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_failure_resets_previous_transfer_on_both_acquisition_paths(self):
        for path in ("buffered", "file"):
            with self.subTest(path=path), private_source_directory() as (root, directory):
                owner, api, store, _ = file_sources()
                client = Client(Stream([b"g"], failure=temporary_read_error()), headers=store_headers(b"geometry"))
                async def acquire():
                    if path == "buffered":
                        return await owner.acquire(api.member.upload_ref)
                    return await owner.acquire_file(api.member.upload_ref, directory_fd=directory, retirement_seconds=0.1)
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
                    await owner.inspect()
                    with self.assertRaises(UploadFailed) as caught:
                        await acquire()
                    first = caught.exception
                    self.assertTrue(owner.can_retry_transfer(first))
                    self.assertEqual(owner.received_bytes, 1)
                    self.assertEqual(owner.requests_started, 4)
                    self.assertEqual(len(client.calls), 1)
                    # Failure still invalidates membership. The predicate sends nothing.
                    with self.assertRaises(UploadInputRejected):
                        await acquire()
                    self.assertFalse(owner.can_retry_transfer(first))
                    self.assertEqual(owner.requests_started, 4)
                    await owner.inspect()
                    api.grant = first
                    with self.assertRaises(UploadFailed) as repeated:
                        await acquire()
                self.assertIs(repeated.exception, first)
                self.assertIs(owner.last_failure, first)
                self.assertFalse(owner.can_retry_transfer(first))
                self.assertEqual(owner.received_bytes, 1)
                self.assertEqual(owner.requests_started, 7)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(list(Path(root).iterdir()), [])


class UploadRetryInterruptionTests(unittest.IsolatedAsyncioTestCase):
    def transfer(self, store):
        return UploadTransfer("job:example", store=store, store_origin="https://store.example",
                              remaining=lambda: 2, before_request=lambda: None, account=lambda _count: None)

    async def test_actual_store_deadline_interruption_refuses_late_temporary_failure(self):
        _, api, _, _ = file_sources()
        store = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=0.03, retirement_seconds=0.1)
        original = temporary_read_error()
        class InterruptedStream(Stream):
            async def __aiter__(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise original
                yield b"unreachable"
        transfer = self.transfer(store)
        async def grant():
            return api.grant
        with private_source_directory() as (_, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool",
            return_value=Client(InterruptedStream([]), headers=store_headers(b"geometry")),
        ):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
            self.assertTrue(store.retirement_confirmed)
            self.assertIs(caught.exception.cause.primary, original)
            self.assertIs(caught.exception.cause.network_failure, original)
            self.assertIsInstance(caught.exception.cause.interruption, TimeoutError)
            self.assertFalse(transfer.can_retry_transfer(caught.exception))

    async def test_retained_cleanup_failure_is_not_admitted_from_supplied_store_retirement_flag(self):
        # Real HttpTransport never reports retirement while its cleanup failed.
        # A supplied compatible store cannot use that flag to override its record.
        original = temporary_read_error()
        outcome = ExchangeFailed("synthetic store failure", primary=original, network_failure=original,
                                 cleanup_failures=(RuntimeError("synthetic cleanup failure"),))
        class SuppliedStore:
            retirement_confirmed = True
            last_failure = outcome
            async def exchange_file(self, *_args, **_kwargs):
                raise outcome
        _, api, _, _ = file_sources()
        transfer = self.transfer(SuppliedStore())
        async def grant():
            return api.grant
        with private_source_directory() as (_, directory):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
            self.assertIs(caught.exception.cause, outcome)
            self.assertFalse(transfer.file_cleanup_pending)
            self.assertFalse(transfer.can_retry_transfer(caught.exception))
