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

from tools.acquisition_bootstrap import refresh, verify
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
        self.issuer.write_bytes(b"old issuer\n")
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
        with patch("tools.acquisition_bootstrap.os.replace", side_effect=OSError("fixture record failure")):
            with self.assertRaisesRegex(OSError, "fixture record failure"):
                self.install()
        self.assertEqual(b"old source\n", (self.bundle / "owner.sh").read_bytes())
        self.assertEqual(b"old issuer\n", self.issuer.read_bytes())
        self.assertEqual([self.bundle], list(self.bundle.parent.iterdir()))
        self.assertFalse(list(self.root.glob(".acquisition-record-*")))

    def test_checkout_group_write_is_not_executable_drift(self):
        self.install()
        (self.bundle / "owner.sh").chmod(0o664)
        verify(self.bundle, self.record)
        (self.bundle / "owner.sh").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "differs"):
            verify(self.bundle, self.record)
