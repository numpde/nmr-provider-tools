"""Bootstrap refresh keeps source and issuer together; Git tracks execute bits."""

from tools.container_guard import require_container

require_container()

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from host.tools.acquisition_bootstrap import refresh, verify
from tests.repository.quality.sources import ROOT


class AcquisitionBootstrapTests(unittest.TestCase):
    def test_checked_in_source_matches_its_qualified_release(self):
        record = json.loads((ROOT / "requirements/acquisition-owner-release.json").read_bytes())
        verify(ROOT / "bootstrap/acquisition", record)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bootstrap" / "acquisition"
        self.bundle.mkdir(parents=True)
        (self.bundle / "owner.sh").write_bytes(b"old source\n")
        self.issuer = self.root / "issuer.json"
        self.previous_record = {"acquisition_source_files": {"owner.sh": {
            "mode": 0o644, "size": len(b"old source\n"),
            "sha256": hashlib.sha256(b"old source\n").hexdigest()}}}
        self.previous_record_bytes = json.dumps(self.previous_record).encode()
        self.issuer.write_bytes(self.previous_record_bytes)
        self.archive = self.root / "acquisition-owner-0.6.2.tar.gz"
        data = b"new source\n"
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo("owner.sh")
            member.mode, member.size = 0o644, len(data)
            archive.addfile(member, io.BytesIO(data))
        self.record = {"files": {self.archive.name: hashlib.sha256(self.archive.read_bytes()).hexdigest()},
                       "acquisition_source_files": {"owner.sh": {
                           "mode": 0o644, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}
        self.record_bytes = json.dumps(self.record).encode()

    def install(self):
        refresh(self.bundle, self.record, self.archive, "0.6.2", self.issuer, self.record_bytes)

    def test_refresh_publishes_matching_source_and_record_without_backup_clutter(self):
        previous_umask = os.umask(0o077)
        try:
            self.install()
        finally:
            os.umask(previous_umask)
        verify(self.bundle, json.loads(self.issuer.read_bytes()))
        self.assertEqual(0o755, self.bundle.stat().st_mode & 0o777)
        self.assertEqual([self.bundle], list(self.bundle.parent.iterdir()))

    def test_record_publication_failure_restores_existing_source_and_issuer(self):
        with patch("host.tools.acquisition_bootstrap.os.replace", side_effect=OSError("fixture record failure")):
            with self.assertRaisesRegex(OSError, "fixture record failure"):
                self.install()
        self.assertEqual(b"old source\n", (self.bundle / "owner.sh").read_bytes())
        self.assertEqual(self.previous_record_bytes, self.issuer.read_bytes())
        self.assertEqual([self.bundle], list(self.bundle.parent.iterdir()))
        self.assertFalse(list(self.root.glob(".acquisition-record-*")))

    def test_checkout_group_write_is_not_executable_drift(self):
        self.install()
        (self.bundle / "owner.sh").chmod(0o664)
        verify(self.bundle, self.record)
        (self.bundle / "owner.sh").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "differs"):
            verify(self.bundle, self.record)

    def test_refresh_preserves_modified_existing_snapshot_and_issuer(self):
        owner = self.bundle / "owner.sh"
        extra = self.bundle / "unexpected"
        for mutation in ("bytes", "execute", "extra"):
            with self.subTest(mutation=mutation):
                owner.write_bytes(b"old source\n")
                owner.chmod(0o644)
                extra.unlink(missing_ok=True)
                if mutation == "bytes":
                    owner.write_bytes(b"local edit\n")
                elif mutation == "execute":
                    owner.chmod(0o755)
                else:
                    extra.write_bytes(b"local member\n")
                previous_bytes = owner.read_bytes()
                previous_mode = owner.stat().st_mode
                with self.assertRaisesRegex(ValueError, "differ.*from the release"):
                    self.install()
                self.assertEqual(previous_bytes, owner.read_bytes())
                self.assertEqual(previous_mode, owner.stat().st_mode)
                self.assertEqual(mutation == "extra", extra.exists())
                if extra.exists():
                    self.assertEqual(b"local member\n", extra.read_bytes())
                self.assertEqual(self.previous_record_bytes, self.issuer.read_bytes())
                self.assertEqual([self.bundle], list(self.bundle.parent.iterdir()))
                self.assertFalse(list(self.root.glob(".acquisition-record-*")))

    def test_refresh_can_remove_a_member_owned_by_the_previous_release(self):
        removed = self.bundle / "removed.sh"
        data = b"previous release member\n"
        removed.write_bytes(data)
        self.previous_record["acquisition_source_files"]["removed.sh"] = {
            "mode": 0o644, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.issuer.write_bytes(json.dumps(self.previous_record).encode())
        self.install()
        verify(self.bundle, self.record)
        self.assertFalse(removed.exists())
        self.assertEqual(self.record_bytes, self.issuer.read_bytes())
        self.assertEqual([self.bundle], list(self.bundle.parent.iterdir()))

    def test_refresh_preserves_an_unrecorded_empty_directory(self):
        local_directory = self.bundle / "local-empty-directory"
        local_directory.mkdir()
        with self.assertRaisesRegex(ValueError, "directories differ"):
            self.install()
        self.assertTrue(local_directory.is_dir())
        self.assertEqual(b"old source\n", (self.bundle / "owner.sh").read_bytes())
        self.assertEqual(self.previous_record_bytes, self.issuer.read_bytes())
        self.assertEqual([self.bundle], list(self.bundle.parent.iterdir()))

    def test_previous_snapshot_cleanup_failure_reports_the_published_replacement(self):
        with patch("host.tools.acquisition_bootstrap.shutil.rmtree", side_effect=OSError("backup cleanup failed")):
            with self.assertRaisesRegex(OSError, "replacement published; cannot remove previous snapshot") as raised:
                self.install()
        self.assertEqual("backup cleanup failed", str(raised.exception.__cause__))
        verify(self.bundle, self.record)
        self.assertEqual(self.record_bytes, self.issuer.read_bytes())
        backups = list(self.bundle.parent.glob(".acquisition-previous-*"))
        self.assertEqual(1, len(backups))
        verify(backups[0], self.previous_record)
