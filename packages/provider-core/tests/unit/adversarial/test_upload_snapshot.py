"""Only the opened object's bounded, grant-matching bytes may reach a parser."""

import asyncio
import errno
import os
import traceback
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from provider_core.uploads import UploadFileReadFailed, read_upload_file
from tests.fakes.acquisition import observe_source_files, private_source_directory, snapshot_source


class UploadSnapshotTests(unittest.TestCase):
    def test_invalid_locator_and_worker_bound_refuse_before_open(self):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            cases = [
                (replace(receipt, name=name), directory, 8)
                for name in ("", ".", "..", "../outside", "/absolute", "sub/source", "source\0")
            ]
            cases.extend((receipt, descriptor, 8) for descriptor in (None, True, -1))
            cases.extend((receipt, directory, limit) for limit in (0, -1, True, 1.5, 7))
            cases.extend((replace(receipt, byte_length=length), directory, 8) for length in (-1, True, "8"))
            for candidate, descriptor, limit in cases:
                with self.subTest(name=candidate.name, descriptor=descriptor, limit=limit):
                    with patch("provider_core.uploads.os.open") as opened, self.assertRaises(UploadFileReadFailed):
                        read_upload_file(candidate, directory_fd=descriptor, maximum_bytes=limit)
                    opened.assert_not_called()
            self.assertEqual(Path(root, receipt.name).read_bytes(), b"original")

    def test_symlink_directory_and_fifo_never_return_a_snapshot(self):
        for kind in ("symlink", "directory", "fifo"):
            with self.subTest(kind=kind), private_source_directory() as (root, directory):
                receipt = snapshot_source(root, b"original")
                path = Path(root, receipt.name)
                path.unlink()
                if kind == "symlink":
                    target = Path(root, "target")
                    target.write_bytes(b"original")
                    path.symlink_to(target)
                elif kind == "directory":
                    path.mkdir()
                else:
                    os.mkfifo(path)
                with observe_source_files(directory) as calls, self.assertRaises(UploadFileReadFailed):
                    read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
                self.assertEqual(calls.closed, calls.descriptors)
                self.assertEqual(calls.removed, [])

    def test_replacement_after_open_reads_the_original_object_once(self):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            path = Path(root, receipt.name)
            opened = os.open
            descriptors = []

            def replace_entry(name, flags, *, dir_fd):
                descriptor = opened(name, flags, dir_fd=dir_fd)
                descriptors.append(descriptor)
                path.replace(Path(root, "retired-original"))
                path.write_bytes(b"replaced")
                return descriptor

            with patch("provider_core.uploads.os.open", side_effect=replace_entry) as opening:
                snapshot = read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            opening.assert_called_once()
            self.assertEqual(snapshot.data, b"original")
            self.assertEqual(path.read_bytes(), b"replaced")
            with self.assertRaises(OSError) as closed:
                os.fstat(descriptors[0])
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_same_length_replacement_before_open_cannot_reach_the_parser(self):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            Path(root, receipt.name).write_bytes(b"replaced")
            parser = Mock()
            with observe_source_files(directory) as calls, self.assertRaisesRegex(UploadFileReadFailed, "SHA-256"):
                parser(read_upload_file(receipt, directory_fd=directory, maximum_bytes=8).data)
            parser.assert_not_called()
            self.assertEqual(calls.closed, calls.descriptors)
            self.assertEqual(calls.removed, [])

    def test_mutation_between_chunks_never_admits_hybrid_bytes(self):
        original = b"a" * 65536 + b"b" * 65536
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, original)
            read = os.read
            observed = []

            def mutate_after_read(descriptor, count):
                chunk = read(descriptor, count)
                observed.append(len(chunk))
                if len(observed) == 1:
                    Path(root, receipt.name).write_bytes(b"c" * len(original))
                return chunk

            with (
                patch("provider_core.uploads.os.read", side_effect=mutate_after_read),
                self.assertRaisesRegex(UploadFileReadFailed, "SHA-256"),
            ):
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=len(original))
            self.assertGreater(len(observed), 1)
            self.assertEqual(sum(observed), len(original))

    def test_growth_and_truncation_after_stat_are_checked_against_actual_bytes(self):
        for changed in (b"short", b"original-plus-more"):
            with self.subTest(changed=changed):
                self.check_changed_size(changed)

    def check_changed_size(self, changed):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            fstat, read = os.fstat, os.read
            consumed = []

            def mutate_after_stat(descriptor):
                result = fstat(descriptor)
                Path(root, receipt.name).write_bytes(changed)
                return result

            def observe(descriptor, count):
                chunk = read(descriptor, count)
                consumed.append(len(chunk))
                return chunk

            with (
                patch("provider_core.uploads.os.fstat", side_effect=mutate_after_stat),
                patch("provider_core.uploads.os.read", side_effect=observe),
                self.assertRaisesRegex(UploadFileReadFailed, "length"),
            ):
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            self.assertLessEqual(sum(consumed), receipt.byte_length + 1)

    def test_read_and_independent_close_failures_both_survive_without_retry_or_unlink(self):
        primary = OSError(errno.EIO, "PRIVATE_SOURCE_PATH")
        cleanup = OSError(errno.EBADF, "PRIVATE_CLOSE_PATH")
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            close = os.close
            calls = []

            def close_then_fail(descriptor):
                calls.append(descriptor)
                close(descriptor)
                raise cleanup

            with (
                patch("provider_core.uploads.os.read", side_effect=primary),
                patch("provider_core.uploads.os.close", side_effect=close_then_fail),
                self.assertRaises(UploadFileReadFailed) as caught,
            ):
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            self.assertIs(caught.exception.cause, primary)
            (close_failure,) = caught.exception.cleanup_failures
            self.assertIs(close_failure.cause, cleanup)
            self.assertIn("retire", str(close_failure).lower())
            self.assertEqual(len(calls), 1)
            self.assertEqual(Path(root, receipt.name).read_bytes(), b"original")
            self.assertNotIn("PRIVATE", str(caught.exception) + str(close_failure))
            self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))

    def test_a_complete_copy_with_unconfirmed_close_is_not_handed_off(self):
        cleanup = OSError(errno.EIO, "PRIVATE_CLOSE_PATH")
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            close = os.close

            def close_then_fail(descriptor):
                close(descriptor)
                raise cleanup

            with (
                patch("provider_core.uploads.os.close", side_effect=close_then_fail) as closing,
                self.assertRaises(UploadFileReadFailed) as caught,
            ):
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            self.assertIs(caught.exception.cause, cleanup)
            self.assertIn("retire", str(caught.exception).lower())
            closing.assert_called_once()
            self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))

    def test_open_failure_keeps_private_cause_out_of_the_ordinary_traceback(self):
        primary = OSError(errno.EACCES, "PRIVATE_PERMISSION_PATH")
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            with patch("provider_core.uploads.os.open", side_effect=primary), self.assertRaises(UploadFileReadFailed) as caught:
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            self.assertIs(caught.exception.cause, primary)
            self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))

    def test_interruption_stays_the_original_exception_when_closure_succeeds(self):
        interruption = asyncio.CancelledError("PRIVATE_INTERRUPTION")
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            with (
                observe_source_files(directory) as calls,
                patch("provider_core.uploads.os.read", side_effect=interruption),
                self.assertRaises(asyncio.CancelledError) as caught,
            ):
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            self.assertIs(caught.exception, interruption)
            self.assertEqual(calls.closed, calls.descriptors)

    def test_interruption_with_close_failure_keeps_original_cancellation_and_previous_cause(self):
        interruption = asyncio.CancelledError("Snapshot interrupted")
        previous = OSError(errno.ENOSPC, "PRIVATE_PREVIOUS_PATH")
        interruption.__cause__ = previous
        cleanup = OSError(errno.EIO, "PRIVATE_CLOSE_PATH")
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            close = os.close

            def close_then_fail(descriptor):
                close(descriptor)
                raise cleanup

            with (
                patch("provider_core.uploads.os.read", side_effect=interruption),
                patch("provider_core.uploads.os.close", side_effect=close_then_fail) as closing,
                self.assertRaises(asyncio.CancelledError) as caught,
            ):
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            self.assertIs(caught.exception, interruption)
            retained = interruption.__cause__
            self.assertIs(type(retained), UploadFileReadFailed)
            self.assertIs(retained.cause, previous)
            self.assertIs(retained.interruption, interruption)
            (close_failure,) = retained.cleanup_failures
            self.assertIs(close_failure.cause, cleanup)
            closing.assert_called_once()
            self.assertNotIn("PRIVATE", "".join(traceback.format_exception(interruption)))
