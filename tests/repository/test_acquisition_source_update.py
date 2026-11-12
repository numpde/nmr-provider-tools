"""A pinned replacement preserves old-source admission and honest publication."""

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

from host.acquisition_tool import source_update as updates


class SourceUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.old = {"acquisition_tool/old.py": b"old source\n"}
        self.new = {"acquisition_tool/new.py": b"new source\n", "compose/worker.yml": b"services: {}\n"}
        self.write_snapshot(self.source, "1.0.0", self.old)
        self.archive = self.root / "candidate.tar.gz"
        self.pin = self.root / "release-pin.json"
        self.write_archive(extra=())

    def manifest(self, version, payload):
        return {
            "release_version": version, "source_revision": "a" * 40,
            "payload_files": {
                name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": 0o644}
                for name, data in payload.items()
            },
            "dependency_inputs": {"wheelhouse-tool-0.9.0.pyz": "b" * 64},
        }

    def write_snapshot(self, root, version, payload):
        for name, data in payload.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        (root / "acquisition-manifest.json").write_text(json.dumps(self.manifest(version, payload)))
        (root / "acquisition-manifest.json").chmod(0o644)

    def write_archive(self, *, extra):
        payload = dict(self.new)
        payload["acquisition-manifest.json"] = json.dumps(self.manifest("2.0.0", self.new)).encode()
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, data in payload.items():
                member = tarfile.TarInfo(name)
                member.mode, member.size = 0o644, len(data)
                archive.addfile(member, io.BytesIO(data))
            for name, kind in extra:
                member = tarfile.TarInfo(name)
                member.mode, member.type = 0o644, kind
                if member.isfile():
                    archive.addfile(member, io.BytesIO())
                else:
                    archive.addfile(member)
        self.pin.write_text(json.dumps({
            "version": "2.0.0", "archive_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
        }))

    def assert_old_source(self):
        self.assertEqual("1.0.0", updates.verify(self.source)["release_version"])
        self.assertEqual(b"old source\n", (self.source / "acquisition_tool/old.py").read_bytes())

    def test_old_snapshot_and_next_pin_replace_membership_without_rewriting_pin(self):
        pin_bytes = self.pin.read_bytes()
        manifest = updates.import_archive(self.source, self.pin, self.archive)
        self.assertEqual("2.0.0", manifest["release_version"])
        self.assertEqual(self.new.keys(), updates.verify(self.source)["payload_files"].keys())
        self.assertFalse((self.source / "acquisition_tool/old.py").exists())
        self.assertEqual(pin_bytes, self.pin.read_bytes())
        self.assertEqual([], list(self.root.glob(".acquisition-next-*")))

    def test_local_edit_refuses_update_before_download(self):
        (self.source / "acquisition_tool/old.py").write_bytes(b"local edit")
        with patch.object(updates.subprocess, "run") as download, self.assertRaises(ValueError):
            updates.update(self.source, self.pin)
        download.assert_not_called()
        self.assertEqual(b"local edit", (self.source / "acquisition_tool/old.py").read_bytes())

    def test_checksum_failure_precedes_tar_parsing_and_preserves_source(self):
        self.archive.write_bytes(b"not an authenticated archive")
        with patch.object(updates.tarfile, "open") as parse, self.assertRaisesRegex(ValueError, "checksum mismatch"):
            updates.import_archive(self.source, self.pin, self.archive)
        parse.assert_not_called()
        self.assert_old_source()

    def test_nonregular_archive_is_rejected_without_waiting_for_input(self):
        fifo = self.root / "archive-fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "regular file"):
            updates.import_archive(self.source, self.pin, fifo)
        self.assert_old_source()

    def test_unsafe_or_duplicate_archive_members_never_publish(self):
        for name, kind in (
            ("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
            ("acquisition_tool/new.py", tarfile.REGTYPE), ("linked", tarfile.SYMTYPE),
            ("special", tarfile.FIFOTYPE), ("acquisition-manifest.json/child", tarfile.REGTYPE),
        ):
            with self.subTest(name=name):
                self.write_archive(extra=((name, kind),))
                with self.assertRaises(ValueError):
                    updates.import_archive(self.source, self.pin, self.archive)
                self.assert_old_source()

    def test_git_checkout_modes_preserve_only_the_executable_contract(self):
        path = self.source / "acquisition_tool/old.py"
        path.chmod(0o664)
        updates.verify(self.source)
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "executable bits"):
            updates.verify(self.source)

    def test_unrecorded_directory_or_symlink_is_not_admitted(self):
        extra = self.source / "extra"
        extra.mkdir()
        with self.assertRaises(ValueError):
            updates.verify(self.source)
        extra.rmdir()
        extra.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            updates.verify(self.source)

    def test_exchange_failure_keeps_old_source_and_retains_candidate_evidence(self):
        failure = PermissionError("exchange denied")
        with patch.object(updates, "_exchange_directories", side_effect=failure), self.assertRaises(PermissionError) as caught:
            updates.import_archive(self.source, self.pin, self.archive)
        self.assertIs(failure, caught.exception)
        self.assert_old_source()
        stages = list(self.root.glob(".acquisition-next-*"))
        self.assertEqual(1, len(stages))
        self.assertEqual("2.0.0", updates.verify(stages[0])["release_version"])
        self.assertIn(str(stages[0]), " ".join(failure.__notes__))

    def test_cleanup_failure_reports_published_source_and_original_error(self):
        failure = PermissionError("previous snapshot cleanup denied")
        with patch.object(updates.shutil, "rmtree", side_effect=failure), self.assertRaises(PermissionError) as caught:
            updates.import_archive(self.source, self.pin, self.archive)
        self.assertIs(failure, caught.exception)
        self.assertEqual("2.0.0", updates.verify(self.source)["release_version"])
        self.assertIn("published", " ".join(failure.__notes__))
        stages = list(self.root.glob(".acquisition-next-*"))
        self.assertEqual("1.0.0", updates.verify(stages[0])["release_version"])

    def test_staging_and_cleanup_failures_both_remain_available(self):
        primary, cleanup = ValueError("candidate stage failed"), PermissionError("stage cleanup failed")
        verify = updates.verify

        def reject_stage(path):
            if Path(path).name.startswith(".acquisition-next-"):
                raise primary
            return verify(path)

        with (
            patch.object(updates, "verify", side_effect=reject_stage),
            patch.object(updates.shutil, "rmtree", side_effect=cleanup),
            self.assertRaises(BaseExceptionGroup) as caught,
        ):
            updates.import_archive(self.source, self.pin, self.archive)
        self.assertEqual((primary, cleanup), caught.exception.exceptions)
        self.assert_old_source()

    def download(self, arguments, *, check):
        self.assertTrue(check)
        self.assertEqual(["gh", "release", "download", "v2.0.0", "--repo", "numpde/nmr-provider-tools"], arguments[:6])
        self.assertEqual(["--pattern", "acquisition-owner-2.0.0.tar.gz"], arguments[-2:])
        destination = Path(arguments[arguments.index("--dir") + 1])
        (destination / arguments[-1]).write_bytes(self.archive.read_bytes())
        self.download_directory = destination

    def test_remote_update_downloads_one_asset_through_the_common_admission_path(self):
        with (
            patch.object(updates.subprocess, "run", side_effect=self.download) as download,
            patch.object(updates, "_admit_archive", wraps=updates._admit_archive) as admission,
        ):
            updates.update(self.source, self.pin)
        self.assertEqual(1, download.call_count)
        self.assertEqual(1, admission.call_count)
        self.assertEqual("2.0.0", updates.verify(self.source)["release_version"])
        self.assertFalse(self.download_directory.exists())

    def test_edit_during_download_is_not_overwritten(self):
        def download_and_edit(arguments, *, check):
            self.download(arguments, check=check)
            (self.source / "acquisition_tool/old.py").write_bytes(b"edit during download")

        with patch.object(updates.subprocess, "run", side_effect=download_and_edit), self.assertRaises(ValueError):
            updates.update(self.source, self.pin)
        self.assertEqual(b"edit during download", (self.source / "acquisition_tool/old.py").read_bytes())
        self.assertFalse(self.download_directory.exists())
        self.assertEqual([], list(self.root.glob(".acquisition-next-*")))
