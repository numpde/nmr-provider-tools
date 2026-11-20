"""A verified transfer hands a private closed file to its composition owner."""

import hashlib
import os
import stat
import unittest
import pathlib
from unittest.mock import patch

from provider_core.uploads import VerifiedUploadFile
from tests.fakes.acquisition import file_sources, private_source_directory, store_headers
from tests.fakes.transport import Client, Stream
from tests.fakes.uploads import UPLOAD_REF


class UploadFileAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_file_handoff_preserves_exact_facts_and_releases_acquisition_owner(self):
        (owner, api, store, client) = file_sources()

        await owner.inspect()

        with (
            private_source_directory() as (root, directory),
            patch("provider_core.adapters.httpcore.httpcore.AsyncConnectionPool", return_value=client),
        ):
            verified_file = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)

            self.assertIs(type(verified_file), VerifiedUploadFile)
            self.assertEqual((verified_file.job_ref, verified_file.upload_ref, verified_file.byte_length), ("job:example", UPLOAD_REF, 8))
            self.assertEqual(verified_file.content_hash, "sha256:" + hashlib.sha256(b"geometry").hexdigest())
            verified_file_path = pathlib.Path(root) / verified_file.name

            self.assertEqual(verified_file_path.read_bytes(), b"geometry")
            self.assertEqual(stat.S_IMODE(verified_file_path.stat().st_mode), 0o600)
            self.assertNotIn(verified_file.name, repr(verified_file))
            self.assertTrue(store.retirement_confirmed)
            self.assertEqual((owner.received_bytes, owner.requests_started), (8, 4))
            self.assertEqual(api.calls, ["membership", "membership", "grant"])
            await owner.inspect()
            self.assertTrue(verified_file_path.exists())
            self.assertEqual(owner.requests_started, 5)
            os.unlink(verified_file.name, dir_fd=directory)

    async def test_file_and_byte_outputs_use_the_same_cumulative_session(self):
        (owner, api, _, file_client) = file_sources(total_bytes=16)
        byte_client = Client(Stream([b"geometry"]), headers=store_headers(b"geometry"))

        await owner.inspect()

        with (
            private_source_directory() as (root, directory),
            patch("provider_core.adapters.httpcore.httpcore.AsyncConnectionPool", side_effect=[file_client, byte_client]) as clients,
        ):
            verified_file = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)

            await owner.inspect()
            verified_upload = await owner.acquire(UPLOAD_REF)

            self.assertEqual(pathlib.Path(root, verified_file.name).read_bytes(), verified_upload.data)
            self.assertEqual((owner.received_bytes, owner.requests_started), (16, 8))
            self.assertEqual(api.calls.count("grant"), 2)
            self.assertEqual(clients.call_count, 2)

    async def test_explicit_private_mode_survives_a_restrictive_creation_umask(self):
        (owner, _, _, client) = file_sources()

        await owner.inspect()

        with (
            private_source_directory() as (root, directory),
            patch("provider_core.adapters.httpcore.httpcore.AsyncConnectionPool", return_value=client),
        ):
            previous = os.umask(0o777)

            try:
                verified_file = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            finally:
                os.umask(previous)

            self.assertEqual(stat.S_IMODE(pathlib.Path(root, verified_file.name).stat().st_mode), 0o600)
