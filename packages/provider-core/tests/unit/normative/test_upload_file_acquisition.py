"""A verified transfer hands a private closed file to its composition owner."""

import hashlib
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.uploads import VerifiedUploadFile
from tests.fakes.acquisition import file_sources, private_source_directory, store_headers
from tests.fakes.transport import Client, Stream
from tests.fakes.uploads import UPLOAD_REF


class UploadFileAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_file_handoff_preserves_exact_facts_and_releases_acquisition_owner(self):
        owner, api, store, client = file_sources()
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            result = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            self.assertIs(type(result), VerifiedUploadFile)
            self.assertEqual((result.job_ref, result.upload_ref, result.byte_length), ("job:example", UPLOAD_REF, 8))
            self.assertEqual(result.content_hash, "sha256:" + hashlib.sha256(b"geometry").hexdigest())
            path = Path(root) / result.name
            self.assertEqual(path.read_bytes(), b"geometry")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(result.name, repr(result))
            self.assertTrue(store.retirement_confirmed)
            self.assertEqual((owner.received_bytes, owner.requests_started), (8, 4))
            self.assertEqual(api.calls, ["membership", "membership", "grant"])
            await owner.inspect()
            self.assertTrue(path.exists())
            self.assertEqual(owner.requests_started, 5)
            os.unlink(result.name, dir_fd=directory)

    async def test_file_and_byte_outputs_use_the_same_cumulative_session(self):
        owner, api, _, file_client = file_sources(total_bytes=16)
        byte_client = Client(Stream([b"geometry"]), headers=store_headers(b"geometry"))
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=[file_client, byte_client]) as clients,
        ):
            result = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            await owner.inspect()
            result_bytes = await owner.acquire(UPLOAD_REF)
            self.assertEqual(Path(root, result.name).read_bytes(), result_bytes.data)
            self.assertEqual((owner.received_bytes, owner.requests_started), (16, 8))
            self.assertEqual(api.calls.count("grant"), 2)
            self.assertEqual(clients.call_count, 2)

    async def test_explicit_private_mode_survives_a_restrictive_creation_umask(self):
        owner, _, _, client = file_sources()
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            previous = os.umask(0o777)
            try:
                result = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(Path(root, result.name).stat().st_mode), 0o600)
