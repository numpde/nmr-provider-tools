"""Retry permission follows the actual completed transfer and its I/O origin."""

import asyncio
import errno
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpcore

from provider_core.uploads import UploadFailed, UploadTransfer
from tests.fakes.acquisition import file_sources, private_source_directory, store_headers
from tests.fakes.transport import Client, Stream


def temporary_read_error():
    failure = httpcore.ReadError("protected network detail")
    failure.__cause__ = ConnectionResetError(errno.ECONNRESET, "protected native detail")
    return failure


class UploadTransferRetryTests(unittest.IsolatedAsyncioTestCase):
    def transfer(self, store, **changes):
        return UploadTransfer(
            "job:example", store=store, store_origin="https://store.example",
            **({"remaining": lambda: 2, "before_request": lambda: None,
                "account": lambda _count: None} | changes),
        )

    async def failed(self, transfer, grant, client, *, directory):
        async def obtain():
            return grant
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(obtain, directory_fd=directory, retirement_seconds=0.1)
        return caught.exception

    async def test_same_read_error_is_retryable_only_from_network_await(self):
        original = temporary_read_error()
        for origin in ("network", "account", "write", "grant", "budget"):
            with self.subTest(origin=origin), private_source_directory() as (root, directory):
                _, api, store, _ = file_sources()
                stream_failure = None
                if origin == "network":
                    stream_failure = original
                client = Client(Stream([b"g"], failure=stream_failure), headers=store_headers(b"geometry"))
                def fail(*_args):
                    raise original
                changes = {}
                if origin == "account":
                    changes["account"] = fail
                elif origin == "budget":
                    changes["before_request"] = fail
                transfer = self.transfer(store, **changes)
                async def grant():
                    if origin == "grant":
                        raise original
                    return api.grant
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client), patch(
                    "provider_core.transport.os.write", side_effect=fail if origin == "write" else os.write
                ):
                    with self.assertRaises(UploadFailed) as caught:
                        await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
                self.assertEqual(transfer.can_retry_transfer(caught.exception), origin == "network")
                self.assertFalse(caught.exception.grant_issued)
                self.assertEqual(list(Path(root).iterdir()), [])
                if origin in {"network", "account", "write"}:
                    self.assertIs(caught.exception.cause.primary, original)
                    self.assertIs(caught.exception.cause.network_failure, stream_failure)

    async def test_connection_timeout_before_any_head_is_retryable(self):
        _, api, store, client = file_sources()
        original = httpcore.ConnectTimeout("protected connection detail")
        transfer = self.transfer(store)
        with private_source_directory() as (_, directory), patch.object(
            client, "handle_async_request", side_effect=original
        ):
            failure = await self.failed(transfer, api.grant, client, directory=directory)
            self.assertIs(failure.cause.primary, original)
            self.assertIs(failure.cause.network_failure, original)
            self.assertTrue(transfer.can_retry_transfer(failure))

    async def test_new_grant_factory_rethrowing_prior_failure_does_not_reuse_store_provenance(self):
        _, api, store, _ = file_sources()
        transfer = self.transfer(store)
        with private_source_directory() as (_, directory):
            failure = await self.failed(transfer, api.grant,
                                        Client(Stream([], failure=temporary_read_error()), headers=store_headers(b"geometry")),
                                        directory=directory)
            self.assertTrue(transfer.can_retry_transfer(failure))
            async def grant():
                raise failure
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1)
            self.assertIs(caught.exception, failure)
            self.assertFalse(transfer.can_retry_transfer(failure))

    async def test_owned_header_refusals_precede_any_error_body_delivery(self):
        for status in (200, 401, 403, 404, 429, 500, 503, 507):
            for incomplete in (False, True):
                with self.subTest(status=status, incomplete=incomplete), private_source_directory() as (_, directory):
                    _, api, store, _ = file_sources()
                    transfer = self.transfer(store)
                    stream_failure = None
                    if incomplete:
                        stream_failure = temporary_read_error()
                    client = Client(Stream([], failure=stream_failure), status=status, headers=[])
                    failure = await self.failed(transfer, api.grant, client, directory=directory)
                    self.assertEqual(transfer.can_retry_transfer(failure),
                                     status in {401, 503, 507})

    async def test_protocol_truncation_requires_exact_valid_whole_file_head(self):
        for header in ("valid", "missing", "wrong-digest", "duplicate", "partial", "transfer-encoding"):
            with self.subTest(header=header), private_source_directory() as (_, directory):
                _, api, store, _ = file_sources()
                headers = list(store_headers(b"geometry"))
                if header == "missing":
                    headers = []
                elif header == "wrong-digest":
                    headers = list(store_headers(b"wronglen"))
                elif header == "duplicate":
                    headers.append((b"content-length", b"8"))
                elif header == "partial":
                    headers.append((b"content-range", b"bytes 0-1/8"))
                elif header == "transfer-encoding":
                    headers.append((b"Transfer-Encoding", b"chunked"))
                transfer = self.transfer(store)
                client = Client(Stream([b"g"], failure=httpcore.RemoteProtocolError("incomplete body")), headers=headers)
                failure = await self.failed(transfer, api.grant, client, directory=directory)
                self.assertEqual(transfer.can_retry_transfer(failure), header == "valid")

    async def test_temporary_read_failure_cannot_hide_wrong_transfer_head(self):
        for headers in ([], list(store_headers(b"geometry")) + [(b"Transfer-Encoding", b"chunked")]):
            with self.subTest(headers=headers), private_source_directory() as (_, directory):
                _, api, store, _ = file_sources()
                transfer = self.transfer(store)
                failure = await self.failed(transfer, api.grant,
                                            Client(Stream([], failure=temporary_read_error()), headers=headers),
                                            directory=directory)
                self.assertFalse(transfer.can_retry_transfer(failure))

    async def test_unproven_network_marker_and_unheaded_protocol_error_do_not_grant_permission(self):
        for original in (httpcore.ReadError("unknown native cause"), httpcore.RemoteProtocolError("bad head")):
            with self.subTest(type=type(original)), private_source_directory() as (_, directory):
                _, api, store, client = file_sources()
                transfer = self.transfer(store)
                with patch.object(client, "handle_async_request", side_effect=original):
                    failure = await self.failed(transfer, api.grant, client, directory=directory)
                self.assertIs(failure.cause.network_failure, original)
                self.assertFalse(transfer.can_retry_transfer(failure))

    async def test_hash_failure_wrong_head_and_noncurrent_failure_never_qualify(self):
        for body, headers in ((b"badbytes", store_headers(b"geometry")),
                              (b"geometry", [])):
            with self.subTest(body=body), private_source_directory() as (_, directory):
                _, api, store, _ = file_sources()
                transfer = self.transfer(store)
                failure = await self.failed(transfer, api.grant, Client(Stream([body]), headers=headers), directory=directory)
                self.assertFalse(transfer.can_retry_transfer(failure))
        with private_source_directory() as (_, directory):
            _, api, store, _ = file_sources()
            transfer = self.transfer(store)
            first = await self.failed(transfer, api.grant, Client(Stream([], failure=temporary_read_error()),
                                      headers=store_headers(b"geometry")), directory=directory)
            self.assertTrue(transfer.can_retry_transfer(first))
            second = await self.failed(transfer, replace(api.grant, content_hash="bad"),
                                       Client(Stream([])), directory=directory)
            self.assertFalse(transfer.can_retry_transfer(first))
            self.assertFalse(transfer.can_retry_transfer(second))

    async def test_pending_cleanup_can_later_be_settled_without_losing_network_cause(self):
        _, api, store, _ = file_sources()
        transfer = self.transfer(store)
        with private_source_directory() as (root, directory):
            with patch("provider_core.uploads.os.unlink", side_effect=OSError(errno.EACCES, "removal refused")):
                failure = await self.failed(transfer, api.grant,
                                            Client(Stream([], failure=temporary_read_error()), headers=store_headers(b"geometry")),
                                            directory=directory)
            self.assertFalse(transfer.can_retry_transfer(failure))
            await transfer.settle_file()
            self.assertTrue(transfer.can_retry_transfer(failure))
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_source_deadline_never_gains_permission_from_temporary_store_failure(self):
        _, api, store, _ = file_sources()
        gate = asyncio.Event()
        deadline = asyncio.get_running_loop().time() + 0.05
        transfer = self.transfer(store, remaining=lambda: deadline - asyncio.get_running_loop().time())
        class DelayedFailure(Stream):
            async def __aiter__(self):
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    raise temporary_read_error()
                yield b"x"
        with private_source_directory() as (_, directory):
            failure = await self.failed(transfer, api.grant, Client(DelayedFailure([]), headers=store_headers(b"geometry")),
                                        directory=directory)
            self.assertFalse(transfer.can_retry_transfer(failure))

    async def test_late_network_failure_after_cancellation_requires_actual_retirement_and_file_settlement(self):
        for late in (False, True):
            with self.subTest(late=late), private_source_directory() as (root, directory):
                _, api, store, _ = file_sources()
                entered, released = asyncio.Event(), asyncio.Event()
                original = temporary_read_error()
                class LateStream(Stream):
                    async def __aiter__(self):
                        entered.set()
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            await released.wait()
                            if late:
                                raise original
                            raise
                        yield b"unreachable"
                transfer = self.transfer(store)
                async def grant():
                    return api.grant
                client = Client(LateStream([]), headers=store_headers(b"geometry"))
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
                    task = asyncio.create_task(transfer.acquire_file(grant, directory_fd=directory, retirement_seconds=0.1))
                    try:
                        await entered.wait()
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        failure = transfer.last_failure
                        self.assertFalse(store.retirement_confirmed)
                        self.assertFalse(transfer.can_retry_transfer(failure))
                    finally:
                        released.set()
                        await store.retire(owner=transfer)
                        await transfer.settle_file()
                self.assertIs(transfer.last_failure, failure)
                self.assertFalse(transfer.can_retry_transfer(failure))
                if late:
                    self.assertIsInstance(failure.cause.interruption, asyncio.CancelledError)
                if late:
                    self.assertIs(failure.cause.primary, original)
                    self.assertIs(failure.cause.network_failure, original)
                self.assertEqual(list(Path(root).iterdir()), [])
