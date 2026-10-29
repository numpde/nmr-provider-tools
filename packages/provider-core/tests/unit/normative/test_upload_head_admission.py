"""A rejected head cannot consume an arbitrary error body under the grant cap."""

import errno
import ssl
import unittest
from dataclasses import replace
from hashlib import sha256
from unittest.mock import patch

import anyio
import httpcore

from provider_core.transport import HttpTransport, temporary_io_failure
from provider_core.uploads import UploadFailed, UploadTransfer
from tests.fakes.acquisition import file_sources, private_source_directory
from tests.fakes.transport import Client, Stream


class UploadHeadAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tiny_grant_refuses_503_before_body_and_retains_exact_head_refusal(self):
        _, api, _, _ = file_sources()
        grant = replace(api.grant, byte_length=1, content_hash="sha256:"+sha256(b"x").hexdigest())
        observed, consumed = [], []
        class ErrorBody(Stream):
            async def __aiter__(self):
                consumed.append(True)
                yield b'{"error":"temporarily unavailable"}'
        client = Client(ErrorBody([]), status=503, headers=[(b"content-type", b"application/json")])
        store = HttpTransport(trust=ssl.create_default_context(), exchange_seconds=1, retirement_seconds=0.1)
        transfer = UploadTransfer("job:example", store=store, store_origin="https://store.example",
                                  remaining=lambda:1, before_request=lambda:None, account=observed.append)
        async def fresh():
            return grant
        with private_source_directory() as (_, directory), patch(
            "provider_core.transport.httpcore.AsyncConnectionPool", return_value=client
        ):
            with self.assertRaises(UploadFailed) as caught:
                await transfer.acquire_file(fresh, directory_fd=directory, retirement_seconds=0.1)
        self.assertTrue(transfer.can_retry_transfer(caught.exception))
        self.assertEqual(consumed, [])
        self.assertEqual(observed, [])
        self.assertTrue(store.retirement_confirmed)
        self.assertFalse(transfer.file_cleanup_pending)
        self.assertIs(caught.exception.cause.primary.response, caught.exception.cause.response_head)
        self.assertIsNone(caught.exception.cause.network_failure)

    def test_read_context_permission_is_marker_specific_bounded_and_explicit_cause_first(self):
        native = ConnectionResetError(errno.ECONNRESET, "protected reset")
        mapped = anyio.BrokenResourceError()
        mapped.__cause__ = native
        mapped.__context__ = PermissionError(errno.EACCES, "incidental permission context")
        original = httpcore.ReadError("protected mapped read")
        original.__context__ = mapped
        self.assertFalse(temporary_io_failure(original, 8))
        self.assertFalse(temporary_io_failure(original, 8, network_failure=httpcore.ReadError()))
        self.assertFalse(temporary_io_failure(original, 2, network_failure=original))
        self.assertTrue(temporary_io_failure(original, 3, network_failure=original))
        original.__cause__ = PermissionError(errno.EACCES, "explicit non-temporary refusal")
        self.assertFalse(temporary_io_failure(original, 8, network_failure=original))
