"""Consumers parse verified immutable bytes, never a mutable source pathname."""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.uploads import VerifiedUpload, read_upload_file
from tests.fakes.acquisition import (
    file_sources,
    observe_source_files,
    private_source_directory,
    snapshot_source,
)
from tests.fakes.uploads import UPLOAD_REF


class UploadSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_acquisition_receipt_becomes_a_closed_immutable_snapshot(self):
        owner, api, _, client = file_sources()
        await owner.inspect()
        with (
            private_source_directory() as (root, directory),
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
        ):
            receipt = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            requests = list(api.calls)
            with observe_source_files(directory) as calls:
                snapshot = read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
                self.assertEqual(calls.closed, calls.descriptors)
                self.assertEqual(len(calls.descriptors), 1)
                self.assertEqual(calls.removed, [])
            self.assertIs(type(snapshot), VerifiedUpload)
            self.assertIs(type(snapshot.data), bytes)
            self.assertEqual(snapshot.data, b"geometry")
            self.assertEqual(
                (snapshot.job_ref, snapshot.upload_ref, snapshot.byte_length, snapshot.content_hash),
                (receipt.job_ref, receipt.upload_ref, receipt.byte_length, receipt.content_hash),
            )
            self.assertEqual(api.calls, requests)
            self.assertTrue(Path(root, receipt.name).exists())

    def test_empty_and_multichunk_files_obey_explicit_read_bounds(self):
        for content in (b"", b"a" * (2 * 65536 + 7)):
            with self.subTest(size=len(content)):
                self.check_read_bounds(content)

    def check_read_bounds(self, content):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, content)
            read = os.read
            requested, received = [], []

            def observe(descriptor, count):
                requested.append(count)
                chunk = read(descriptor, count)
                received.append(len(chunk))
                return chunk

            with patch("provider_core.uploads.os.read", side_effect=observe):
                result = read_upload_file(receipt, directory_fd=directory, maximum_bytes=max(1, len(content)))
            self.assertEqual(result.data, content)
            self.assertTrue(all(0 < count <= 65536 for count in requested))
            self.assertLessEqual(sum(received), len(content) + 1)
            self.assertEqual(received[-1], 0)

    def test_source_changes_after_verification_do_not_change_the_consumed_document(self):
        content = b'{"atom_order":["H","C"]}'
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, content)
            snapshot = read_upload_file(receipt, directory_fd=directory, maximum_bytes=100)
            Path(root, receipt.name).write_bytes(b"changed after verification")
            self.assertEqual(json.loads(snapshot.data), {"atom_order": ["H", "C"]})
            self.assertEqual(snapshot.data, content)
