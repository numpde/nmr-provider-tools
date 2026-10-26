"""Pinned SECS archive refusals survive immutable-file receiving adaptation."""

import errno
import stat
import struct
import unittest
import warnings
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZIP_BZIP2, ZIP_LZMA, ZipExtFile, ZipFile, ZipInfo

from provider_core.uploads import UploadFileReadFailed, read_upload_file
from tests.fakes.acquisition import private_source_directory, snapshot_source
from tests.fakes.secs_sources import InputReadError, ScopeLimitError, SourceAccess, SourceRef, _read_chunk
from tests.fakes.uploads import UPLOAD_REF
from tests.integration.uploads.fixtures import zip_bytes


class SecsSourceReceivingTests(unittest.TestCase):
    def test_maximum_inventory_reuses_one_verified_copy_archive_and_member_index(self):
        # repo-check: allow-tuple-generator -- The immutable archive-entry specimen exercises the member-count ceiling.
        body = zip_bytes(tuple((str(index), b"") for index in range(4096)))
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, body)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
            with (
                patch("tests.fakes.secs_sources.read_upload_file", wraps=read_upload_file) as snapshots,
                patch.object(access, "_archive", wraps=access._archive) as archives,
                patch.object(access, "_member", wraps=access._member) as scans,
                access.scope_reader(SourceRef(UPLOAD_REF)) as reader,
            ):
                self.assertEqual(len(reader.entries), 4096)
                for entry in reader.entries:
                    self.assertEqual(reader.read(entry.source).contents, b"")
            snapshots.assert_called_once()
            archives.assert_called_once()
            scans.assert_not_called()

    def test_direct_scope_uses_one_verified_copy_even_after_same_size_path_replacement(self):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=8)
            with (
                patch("tests.fakes.secs_sources.read_upload_file", wraps=read_upload_file) as snapshots,
                access.scope_reader(SourceRef(UPLOAD_REF)) as reader,
            ):
                Path(root, receipt.name).write_bytes(b"replaced")
                self.assertEqual(reader.read(SourceRef(UPLOAD_REF)).contents, b"original")
            snapshots.assert_called_once()

    def test_zip_sniff_preflight_and_member_read_use_the_copy_taken_before_path_mutation(self):
        original = zip_bytes((("value.json", b'{"source":"original"}'),))
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, original)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(original))

            def change_after_snapshot(*arguments, **options):
                snapshot = read_upload_file(*arguments, **options)
                Path(root, receipt.name).write_bytes(b"x" * len(original))
                return snapshot

            with patch("tests.fakes.secs_sources.read_upload_file", side_effect=change_after_snapshot) as snapshots:
                with access.scope_reader(SourceRef(UPLOAD_REF)) as reader:
                    self.assertEqual(reader.read(reader.entries[0].source).contents, b'{"source":"original"}')
                    archive = reader._archive
                self.assertIsNone(archive.fp)
            snapshots.assert_called_once()

    def test_bad_source_identity_refuses_before_any_archive_parser(self):
        body = zip_bytes((("value", b"original"),))
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, body)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
            Path(root, receipt.name).write_bytes(b"x" * len(body))
            with (
                patch("tests.fakes.secs_sources.is_zipfile") as sniff,
                patch("tests.fakes.secs_sources.ZipFile") as archive,
                self.assertRaises(UploadFileReadFailed),
            ):
                access.scope(SourceRef(UPLOAD_REF))
            sniff.assert_not_called()
            archive.assert_not_called()

    def test_mismatched_upload_label_is_operational_but_missing_selection_is_correctable(self):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            other = "upload:sha256:" + "b" * 64
            access = SourceAccess(
                {UPLOAD_REF: replace(receipt, upload_ref=other)}, Path(root), directory_fd=directory, max_upload_bytes=8
            )
            with patch("tests.fakes.secs_sources.read_upload_file") as snapshot:
                with self.assertRaises(UploadFileReadFailed):
                    access.scope(SourceRef(UPLOAD_REF))
                with self.assertRaises(InputReadError):
                    access.scope(SourceRef(other))
            snapshot.assert_not_called()

    def test_one_scope_charges_repeated_reads_without_resetting_the_expanded_budget(self):
        body = zip_bytes((("first", b"123456"), ("second", b"1234")))
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, body)
            access = SourceAccess(
                {UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body), max_scope_bytes=10
            )
            with access.scope_reader(SourceRef(UPLOAD_REF)) as reader:
                self.assertEqual(reader.read(SourceRef(UPLOAD_REF, "first")).contents, b"123456")
                with self.assertRaises(ScopeLimitError):
                    reader.read(SourceRef(UPLOAD_REF, "first"))
                self.assertEqual(reader.read(SourceRef(UPLOAD_REF, "second")).contents, b"1234")
                with self.assertRaises(ScopeLimitError):
                    reader.read(SourceRef(UPLOAD_REF, "second"))

    def test_member_and_total_expansion_limits_remain_distinct(self):
        body = zip_bytes((("first", b"123456"), ("second", b"1234")))
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, body)
            member_limited = SourceAccess(
                {UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body), max_member_bytes=5
            )
            with self.assertRaisesRegex(InputReadError, "member limit"):
                member_limited.read(SourceRef(UPLOAD_REF, "first"))
            scope_limited = SourceAccess(
                {UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body), max_scope_bytes=9
            )
            with self.assertRaisesRegex(InputReadError, "members exceed"):
                scope_limited.scope(SourceRef(UPLOAD_REF))

    def test_excessive_central_directory_is_rejected_before_zipfile_construction(self):
        # A declared count is enough for the original bounded preflight to refuse.
        body = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 4097, 4097, 0, 0, 0)
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, body)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
            with patch("tests.fakes.secs_sources.ZipFile") as archive, self.assertRaisesRegex(InputReadError, "4096"):
                access.read(SourceRef(UPLOAD_REF, "chosen"))
            archive.assert_not_called()

    def test_zip64_and_multidisk_preflight_refusals_still_precede_archive_construction(self):
        cases = ((0, 65535, "ZIP64"), (1, 1, "multi-disk"))
        for disk, count, reason in cases:
            with self.subTest(reason=reason), private_source_directory() as (root, directory):
                body = struct.pack("<4s4H2LH", b"PK\x05\x06", disk, disk, count, count, 0, 0, 0)
                receipt = snapshot_source(root, body)
                access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
                with patch("tests.fakes.secs_sources.ZipFile") as archive, self.assertRaisesRegex(InputReadError, reason):
                    access.read(SourceRef(UPLOAD_REF, "chosen"))
                archive.assert_not_called()

    def test_listing_ceiling_does_not_disable_an_exact_member(self):
        # repo-check: allow-tuple-generator -- The immutable archive-entry specimen exercises expanded listing byte limits.
        entries = tuple(("x" * 200 + str(index), b"v") for index in range(1300))
        body = zip_bytes(entries)
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, body)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
            with self.assertRaisesRegex(InputReadError, "262144-byte inspection limit"):
                access.scope(SourceRef(UPLOAD_REF))
            self.assertEqual(access.read(SourceRef(UPLOAD_REF, entries[-1][0])).contents, b"v")

    def test_duplicate_nonregular_and_missing_members_remain_input_refusals(self):
        link = ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
            duplicate = zip_bytes((("same", b"first"), ("same", b"second")))
        for body, name in (
            (duplicate, "same"),
            (zip_bytes(((link, b"target"),)), "link"),
            (zip_bytes((("present", b"value"),)), "missing"),
        ):
            with self.subTest(member=name), private_source_directory() as (root, directory):
                receipt = snapshot_source(root, body)
                access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
                with self.assertRaises(InputReadError):
                    access.read(SourceRef(UPLOAD_REF, name))

    def test_encrypted_members_refuse_before_a_decoder_is_opened(self):
        body = bytearray(zip_bytes((("encrypted", b"value"),)))
        central = body.index(b"PK\x01\x02")
        struct.pack_into("<H", body, central + 8, 1)
        struct.pack_into("<H", body, 6, 1)
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, bytes(body))
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
            with patch.object(ZipFile, "open") as decoder, self.assertRaisesRegex(InputReadError, "encrypted"):
                access.read(SourceRef(UPLOAD_REF, "encrypted"))
            decoder.assert_not_called()

    def test_malformed_zip_name_encoding_stays_a_correctable_input_refusal(self):
        for header in (b"PK\x01\x02", b"PK\x03\x04"):
            with self.subTest(header=header), private_source_directory() as (root, directory):
                raw = bytearray(zip_bytes((("é.jdx", b"spectrum"),)))
                start = raw.index("é.jdx".encode(), raw.index(header))
                raw[start] = 0xFF
                receipt = snapshot_source(root, bytes(raw))
                access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(raw))
                with self.assertRaises(InputReadError):
                    access.read(SourceRef(UPLOAD_REF, "é.jdx"))

    def test_consumer_failure_closes_every_view_without_becoming_an_input_refusal(self):
        body = zip_bytes((("value", b"original"),))
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, body)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
            views = []

            def observe_view(contents):
                stream = BytesIO(contents)
                views.append(stream)
                return stream

            consumer_failure = RuntimeError("Authored consumer defect")
            with (
                patch("tests.fakes.secs_sources.BytesIO", side_effect=observe_view),
                self.assertRaises(RuntimeError) as caught,
                access.scope_reader(SourceRef(UPLOAD_REF)) as reader,
            ):
                archive = reader._archive
                raise consumer_failure
            self.assertIs(caught.exception, consumer_failure)
            self.assertIsNone(archive.fp)
            self.assertTrue(views)
            self.assertTrue(all(stream.closed for stream in views))

    def test_corrupt_bzip_and_lzma_members_are_input_refusals(self):
        for compression, offset in ((ZIP_BZIP2, 0), (ZIP_LZMA, 4)):
            with self.subTest(compression=compression), private_source_directory() as (root, directory):
                with BytesIO() as output:
                    with ZipFile(output, "w", compression=compression) as archive:
                        archive.writestr("spectrum.jdx", "spectrum " * 300)
                    raw = bytearray(output.getvalue())
                payload = 30 + int.from_bytes(raw[26:28], "little") + int.from_bytes(raw[28:30], "little")
                raw[payload + offset] ^= 255
                receipt = snapshot_source(root, bytes(raw))
                access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(raw))
                with self.assertRaises(InputReadError):
                    access.read(SourceRef(UPLOAD_REF, "spectrum.jdx"))

    def test_failed_decoding_keeps_its_scope_charge_before_another_member_is_read(self):
        with BytesIO() as output:
            with ZipFile(output, "w", compression=ZIP_BZIP2) as archive:
                archive.writestr("damaged", b"123456")
                archive.writestr("second", b"1234")
            raw = bytearray(output.getvalue())
        payload = 30 + int.from_bytes(raw[26:28], "little") + int.from_bytes(raw[28:30], "little")
        raw[payload] ^= 255
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, bytes(raw))
            access = SourceAccess(
                {UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(raw), max_scope_bytes=10
            )
            with access.scope_reader(SourceRef(UPLOAD_REF)) as reader:
                with self.assertRaises(InputReadError):
                    reader.read(SourceRef(UPLOAD_REF, "damaged"))
                with self.assertRaises(ScopeLimitError):
                    reader.read(SourceRef(UPLOAD_REF, "damaged"))
                self.assertEqual(reader.read(SourceRef(UPLOAD_REF, "second")).contents, b"1234")

    def test_filesystem_failures_are_never_reclassified_as_malformed_input(self):
        for stream, error in ((Mock(spec=ZipExtFile), OSError(errno.EIO, "PRIVATE_DISK")), (Mock(), OSError("PRIVATE_READ"))):
            with self.subTest(error=type(error).__name__):
                stream.read.side_effect = error
                with self.assertRaises(OSError) as caught:
                    _read_chunk(stream, 1024)
                self.assertIs(caught.exception, error)
