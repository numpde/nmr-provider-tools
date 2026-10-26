"""A proposed SECS receiving seam consumes installed core's verified bytes.

This is local boundary compatibility, not SECS adoption, async worker retirement
or scientific qualification. The candidate preserves the pinned archive policy.
"""

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from provider_core.uploads import read_upload_file
from tests.fakes.acquisition import file_sources, private_source_directory, snapshot_source, store_headers
from tests.fakes.secs_sources import SourceAccess, SourceRef
from tests.fakes.transport import Client, Stream
from tests.fakes.uploads import UPLOAD_REF
from tests.integration.uploads.fixtures import zip_bytes


class SecsSourceReceivingTests(unittest.IsolatedAsyncioTestCase):
    async def test_acquired_zip_receipt_reaches_the_existing_member_consumption_contract(self):
        body = zip_bytes((("first.json", b'{"record":1}'), ("second.json", b'{"record":2}')))
        owner, api, transport, _ = file_sources(source_bytes=len(body), total_bytes=len(body))
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        api.member = replace(api.member, byte_length=len(body), content_hash=digest)
        api.grant = replace(api.grant, byte_length=len(body), content_hash=digest)
        client = Client(Stream([body]), headers=store_headers(body))
        await owner.inspect()
        with private_source_directory() as (root, directory), TemporaryDirectory() as scratch:
            with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
                receipt = await owner.acquire_file(UPLOAD_REF, directory_fd=directory, retirement_seconds=0.1)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(scratch), directory_fd=directory, max_upload_bytes=len(body))
            with (
                patch("tests.fakes.secs_sources.read_upload_file", wraps=read_upload_file) as snapshots,
                access.scope_reader(SourceRef(UPLOAD_REF)) as reader,
            ):
                self.assertEqual([entry.source.member for entry in reader.entries], ["first.json", "second.json"])
                documents = [json.loads(reader.read(entry.source).contents) for entry in reader.entries]
            self.assertEqual(documents, [{"record": 1}, {"record": 2}])
            snapshots.assert_called_once_with(receipt, directory_fd=directory, maximum_bytes=len(body))
            self.assertEqual((owner.received_bytes, owner.requests_started), (len(body), 4))
            self.assertTrue(transport.retirement_confirmed)
            self.assertTrue(Path(root, receipt.name).exists())

    def test_direct_source_retains_the_existing_readsource_identity_and_digest(self):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b'{"record":1}')
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=100)
            source = SourceRef(UPLOAD_REF)
            with access.scope_reader(source) as reader:
                self.assertEqual([(entry.source, entry.byte_length) for entry in reader.entries], [(source, 12)])
                read = reader.read(source)
            self.assertEqual(read.source, source)
            self.assertEqual(read.contents, b'{"record":1}')
            self.assertEqual(read.digest, receipt.content_hash)

    def test_member_names_are_logical_identities_without_extraction_authority(self):
        body = zip_bytes((("../outside.json", b'{"record":1}'),))
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, body)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(root), directory_fd=directory, max_upload_bytes=len(body))
            with patch.object(ZipFile, "extract") as extract, patch.object(ZipFile, "extractall") as extractall:
                read = access.read(SourceRef(UPLOAD_REF, "../outside.json"))
            extract.assert_not_called()
            extractall.assert_not_called()
            self.assertEqual(json.loads(read.contents), {"record": 1})
            self.assertEqual(list(Path(root).iterdir()), [Path(root, receipt.name)])

    def test_existing_format_consumer_materializes_verified_bytes_in_its_separate_scratch_grant(self):
        # Pinned input_formats.py::prepare_representation writes current[0].contents
        # to spectrum.jdx inside TemporaryDirectory(dir=access.directory). No codec
        # or scientific interpretation is exercised by retaining that boundary here.
        body = zip_bytes((("../untrusted-name.jdx", b"authored spectrum bytes"),))
        with private_source_directory() as (root, directory), TemporaryDirectory() as scratch:
            receipt = snapshot_source(root, body)
            access = SourceAccess({UPLOAD_REF: receipt}, Path(scratch), directory_fd=directory, max_upload_bytes=len(body))
            read = access.read(SourceRef(UPLOAD_REF, "../untrusted-name.jdx"))
            with TemporaryDirectory(dir=access.directory, prefix="jcamp-") as temporary:
                path = Path(temporary) / "spectrum.jdx"
                path.write_bytes(read.contents)
                self.assertEqual(path.read_bytes(), b"authored spectrum bytes")
                self.assertTrue(path.is_relative_to(scratch))
                self.assertFalse(path.is_relative_to(root))
            self.assertFalse(path.exists())
            self.assertEqual(Path(root, receipt.name).read_bytes(), body)
