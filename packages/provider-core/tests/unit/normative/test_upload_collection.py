"""Collection admission precedes I/O; consumers receive only verified bytes."""

from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from provider_core.upload_file import UploadFileReadFailed, read_upload_files
from tests.fakes.acquisition import observe_source_files, private_source_directory, snapshot_source


class UploadCollectionTests(unittest.TestCase):
    def read(self, receipts, directory, **limits):
        return read_upload_files(
            receipts, job_ref="job:example", directory_fd=directory,
            **({"maximum_bytes": 10, "maximum_total_bytes": 10} | limits),
        )

    def test_collection_returns_immutable_verified_snapshots(self):
        with private_source_directory() as (root, directory):
            first = snapshot_source(root, b"first")
            Path(root, first.name).rename(Path(root, "first"))
            first = replace(first, name="first")
            second = replace(snapshot_source(root, b"next"), upload_ref="upload:second")
            receipts = {first.upload_ref: first, second.upload_ref: second}
            snapshots = self.read(receipts, directory)
            Path(root, first.name).write_bytes(b"changed")
            self.assertEqual([snapshot.data for snapshot in snapshots.values()], [b"first", b"next"])

    def test_all_identities_and_sizes_are_checked_before_any_open(self):
        with private_source_directory() as (root, directory):
            first = snapshot_source(root, b"first")
            second = replace(first, upload_ref="upload:second")
            for bad in (
                replace(second, job_ref="job:other"),
                replace(second, job_ref=None),
                replace(second, upload_ref=None),
                replace(second, upload_ref="upload:other"),
                replace(second, byte_length=-1),
                replace(second, byte_length=True),
                replace(second, byte_length=11),
            ):
                with self.subTest(receipt=bad), patch("os.open") as opened:
                    with self.assertRaises(UploadFileReadFailed):
                        self.read({first.upload_ref: first, second.upload_ref: bad}, directory)
                    opened.assert_not_called()
            with patch("os.open") as opened:
                with self.assertRaises(UploadFileReadFailed):
                    self.read({first.upload_ref: first, second.upload_ref: second}, directory,
                              maximum_total_bytes=9)
                opened.assert_not_called()

    def test_later_corruption_fails_collection(self):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"first")
            other = replace(receipt, upload_ref="upload:second", content_hash="sha256:" + "0" * 64)
            with observe_source_files(directory) as calls:
                with self.assertRaises(UploadFileReadFailed):
                    self.read({receipt.upload_ref: receipt, other.upload_ref: other}, directory)
                self.assertEqual(calls.closed, calls.descriptors)
                self.assertEqual(len(calls.descriptors), 2)

    def test_empty_collection_returns_no_snapshots(self):
        with private_source_directory() as (_, directory):
            self.assertEqual(self.read({}, directory), {})

    def test_limits_must_be_positive_integers_even_for_empty_collections(self):
        for field in ("maximum_bytes", "maximum_total_bytes"):
            for value in (True, 0, -1, 1.5):
                with self.subTest(field=field, value=value), patch("os.open") as opened:
                    with self.assertRaises(UploadFileReadFailed):
                        self.read({}, 0, **{field: value})
                    opened.assert_not_called()
