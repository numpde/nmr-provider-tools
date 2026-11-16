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
    def test_manifest_distinguishes_record_types_from_unsupported_values(self):
        manifest = self.manifest("1.0.0", self.old)

        for changes in ({"source_revision": []}, {"payload_files": []}, {"dependency_inputs": []}):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                updates.read_manifest(json.dumps(manifest | changes))

        for changes in ({"source_revision": "bad"}, {"payload_files": {}}, {"dependency_inputs": {"wheel.pyz": "bad"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                updates.read_manifest(json.dumps(manifest | changes))

    def test_manifest_reports_invalid_payload_metadata_fields(self):
        member_name = "acquisition_tool/old.py"

        for (field_changes, exception_type, diagnostic) in (
            ({"sha256": "bad"}, ValueError, "sha256"),
            ({"sha256": []}, ValueError, "sha256"),
            ({"size": -1}, ValueError, "entry_size=-1"),
            ({"size": True}, TypeError, "entry_size"),
            ({"mode": 0o600}, ValueError, "entry_mode=600 (octal); JSON mode must be 420 or 493"),
            ({"mode": True}, TypeError, "entry_mode"),
        ):
            with self.subTest(field_changes=field_changes):
                manifest = self.manifest("1.0.0", self.old)

                manifest["payload_files"][member_name].update(field_changes)

                with self.assertRaises(exception_type) as caught:
                    updates.read_manifest(json.dumps(manifest))

                self.assertIn(diagnostic, str(caught.exception))
                self.assertIn(member_name, str(caught.exception))

        manifest = self.manifest("1.0.0", self.old)

        del manifest["payload_files"][member_name]["sha256"]

        with self.assertRaisesRegex(ValueError, "metadata must contain exactly sha256, size and mode"):
            updates.read_manifest(json.dumps(manifest))

    def test_manifest_identifies_inventory_conflicts(self):
        manifest = self.manifest("1.0.0", self.old)
        metadata = manifest["payload_files"]["acquisition_tool/old.py"]

        for (payload_files, expected_message) in (
            ({}, "payload inventory must not be empty"),
            ({"acquisition-manifest.json": metadata}, "must not include the manifest itself"),
            ({"ancestor": metadata, "ancestor/child": metadata}, "acquisition_rel_path='ancestor/child' has an inventoried parent=PurePosixPath('ancestor')"),
        ):
            with self.subTest(payload_files=payload_files):
                with self.assertRaises(ValueError) as caught:
                    updates.read_manifest(json.dumps(manifest | {"payload_files": payload_files}))

                self.assertIn(expected_message, str(caught.exception))

    def test_manifest_reports_the_invalid_release_identity(self):
        for (changes, expected_requirement, expected_field) in (
            ({"release_version": "latest"}, "must be X.Y.Z in ASCII digits without leading zeroes", "manifest_release_version='latest'"),
            ({"source_revision": "not-a-revision"}, "revision must be 40 lowercase hexadecimal characters", "manifest_source_revision='not-a-revision'"),
        ):
            with self.subTest(changes=changes):
                manifest = self.manifest("1.0.0", self.old)

                with self.assertRaises(ValueError) as caught:
                    updates.read_manifest(json.dumps(manifest | changes))

                self.assertIn(expected_field, str(caught.exception))
                self.assertIn(expected_requirement, str(caught.exception))

    def test_manifest_identifies_invalid_dependency_metadata(self):
        for (dependencies, expected_diagnostic) in (
            ({"": "a" * 64}, "dependency_name=''"),
            ({"wheel.pyz": "bad"}, "dependency_name='wheel.pyz': dependency_sha256='bad'"),
        ):
            with self.subTest(dependencies=dependencies):
                manifest = self.manifest("1.0.0", self.old)
                manifest["dependency_inputs"] = dependencies

                with self.assertRaises(ValueError) as caught:
                    updates.read_manifest(json.dumps(manifest))

                self.assertIn(expected_diagnostic, str(caught.exception))

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
            "release_version": version,
            "source_revision": "a" * 40,
            "payload_files": {
                name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": 0o644}
                for (name, data) in payload.items()
            },
            "dependency_inputs": {"wheelhouse-tool-0.9.0.pyz": "b" * 64},
        }

    def write_snapshot(self, root, version, payload):
        for (name, data) in payload.items():
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
            for (name, data) in payload.items():
                member = tarfile.TarInfo(name)
                (member.mode, member.size) = (0o644, len(data))

                archive.addfile(member, io.BytesIO(data))

            for (name, kind) in extra:
                member = tarfile.TarInfo(name)
                (member.mode, member.type) = (0o644, kind)

                if member.isfile():
                    archive.addfile(member, io.BytesIO())
                else:
                    archive.addfile(member)

        self.pin.write_text(json.dumps({
            "version": "2.0.0",
            "archive_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
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

    def test_release_pin_errors_identify_the_invalid_field(self):
        for (release_pin, expected_message) in (
            ({"version": "2.0.0"}, "require exactly version and archive_sha256"),
            ({"version": "latest", "archive_sha256": "a" * 64}, "must be X.Y.Z in ASCII digits without leading zeroes"),
            ({"version": "2.0.0", "archive_sha256": "bad"}, "archive_sha256"),
        ):
            with self.subTest(release_pin=release_pin):
                self.pin.write_text(json.dumps(release_pin))

                with self.assertRaises(ValueError) as caught:
                    updates.read_pin(self.pin)

                self.assertIn(expected_message, str(caught.exception))
                self.assertIn(str(self.pin), str(caught.exception))

    def test_directory_scan_failure_is_propagated(self):
        acquisition_directory = self.source / "acquisition_tool"
        scan_failure = PermissionError(13, "Permission denied", str(acquisition_directory))
        original_scandir = os.scandir

        def scan_directory(directory):
            if os.fspath(directory) == str(acquisition_directory):
                raise scan_failure

            return original_scandir(directory)

        with patch.object(os, "scandir", side_effect=scan_directory), self.assertRaises(PermissionError) as caught:
            updates.verify(self.source)

        self.assertIs(scan_failure, caught.exception)

    def test_payload_mismatch_identifies_size_or_digest(self):
        acquisition_file = self.source / "acquisition_tool/old.py"
        recorded_bytes = self.old["acquisition_tool/old.py"]
        release_sha256 = hashlib.sha256(recorded_bytes).hexdigest()

        acquisition_file.write_bytes(recorded_bytes + b"x")

        with self.assertRaises(ValueError) as caught:
            updates.verify(self.source)

        self.assertIn("acquisition_rel_path='acquisition_tool/old.py'", str(caught.exception))
        self.assertIn(f"actual_size={len(recorded_bytes) + 1} ≠ release_size={len(recorded_bytes)}", str(caught.exception))

        modified_bytes = b"X" + recorded_bytes[1:]

        acquisition_file.write_bytes(modified_bytes)

        with self.assertRaises(ValueError) as caught:
            updates.verify(self.source)

        self.assertIn("acquisition_rel_path='acquisition_tool/old.py'", str(caught.exception))
        self.assertIn(f"actual_sha256='{hashlib.sha256(modified_bytes).hexdigest()}'", str(caught.exception))
        self.assertIn(f"release_sha256='{release_sha256}'", str(caught.exception))

    def test_snapshot_reports_unobserved_recorded_files(self):
        (self.source / "acquisition_tool/old.py").unlink()

        with self.assertRaisesRegex(ValueError, r"unobserved_files=\['acquisition_tool/old\.py'\]"):
            updates.verify(self.source)

    def test_checksum_failure_precedes_tar_parsing_and_preserves_source(self):
        self.archive.write_bytes(b"not an authenticated archive")

        with patch.object(updates.tarfile, "open") as parse, self.assertRaisesRegex(ValueError, "checksum mismatch"):
            updates.import_archive(self.source, self.pin, self.archive)

        parse.assert_not_called()
        self.assert_old_source()

    def test_reviewed_digest_authenticates_archive_without_a_version_comparison(self) -> None:
        release_pin = json.loads(self.pin.read_text())
        release_pin["version"] = "9.0.0"

        self.pin.write_text(json.dumps(release_pin))
        manifest = updates.import_archive(source_dir=self.source, pin_file=self.pin, archive=self.archive)

        self.assertEqual(first="2.0.0", second=manifest["release_version"])
        self.assertEqual(first="2.0.0", second=updates.verify(self.source)["release_version"])

    def test_nonregular_archive_is_rejected_without_waiting_for_input(self):
        fifo = self.root / "archive-fifo"

        os.mkfifo(fifo)

        with self.assertRaisesRegex(ValueError, "regular file"):
            updates.import_archive(self.source, self.pin, fifo)

        self.assert_old_source()

    def test_archive_metadata_errors_report_the_actual_mismatch(self):
        member_name = "acquisition_tool/new.py"
        member_bytes = self.new[member_name]

        for (declared_size, declared_mode, mismatch) in (
            (len(member_bytes) + 1, 0o644, "size"),
            (len(member_bytes), 0o755, "mode"),
            (len(member_bytes) + 1, 0o755, "size"),
        ):
            with self.subTest(mismatch=mismatch, declared_size=declared_size, declared_mode=declared_mode):
                manifest = self.manifest("2.0.0", self.new)

                manifest["payload_files"][member_name].update(size=declared_size, mode=declared_mode)
                payload = self.new | {"acquisition-manifest.json": json.dumps(manifest).encode()}

                with tarfile.open(self.archive, "w:gz") as archive:
                    for (archive_name, archive_bytes) in payload.items():
                        archive_member = tarfile.TarInfo(archive_name)
                        (archive_member.mode, archive_member.size) = (0o644, len(archive_bytes))

                        archive.addfile(archive_member, io.BytesIO(archive_bytes))

                pin = json.loads(self.pin.read_text())
                pin["archive_sha256"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()

                self.pin.write_text(json.dumps(pin))

                with self.assertRaises(ValueError) as caught:
                    updates.import_archive(self.source, self.pin, self.archive)

                diagnostic = str(caught.exception)

                self.assertIn(f"member.{mismatch}=", diagnostic)

                if mismatch == "mode":
                    self.assertIn("(octal)", diagnostic)
                other_field = {"size": "member.mode=", "mode": "member.size="}[mismatch]

                self.assertNotIn(other_field, diagnostic)
                self.assert_old_source()
                self.assertEqual([], list(self.root.glob(".acquisition-next-*")))

    def test_archive_manifest_mode_is_reported_before_manifest_parsing(self):
        with tarfile.open(self.archive, "w:gz") as archive:
            archive_member = tarfile.TarInfo("acquisition-manifest.json")
            (archive_member.mode, archive_member.size) = (0o755, 1)

            archive.addfile(archive_member, io.BytesIO(b"{"))

        pin = json.loads(self.pin.read_text())
        pin["archive_sha256"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()

        self.pin.write_text(json.dumps(pin))

        with self.assertRaisesRegex(ValueError, "manifest_member.mode=0755 ≠ 0644"):
            updates.import_archive(self.source, self.pin, self.archive)

        self.assert_old_source()
        self.assertEqual([], list(self.root.glob(".acquisition-next-*")))

    def test_archive_reports_missing_and_unrecorded_members(self):
        self.write_archive(extra=(("unexpected", tarfile.REGTYPE),))

        with self.assertRaisesRegex(ValueError, r"unrecorded_members=\['unexpected'\]"):
            updates.import_archive(self.source, self.pin, self.archive)

        self.assert_old_source()
        manifest_bytes = json.dumps(self.manifest("2.0.0", self.new)).encode()

        with tarfile.open(self.archive, "w:gz") as archive:
            for (archive_name, archive_bytes) in (
                ("acquisition-manifest.json", manifest_bytes),
                ("compose/worker.yml", self.new["compose/worker.yml"]),
            ):
                archive_member = tarfile.TarInfo(archive_name)
                (archive_member.mode, archive_member.size) = (0o644, len(archive_bytes))

                archive.addfile(archive_member, io.BytesIO(archive_bytes))

        pin = json.loads(self.pin.read_text())
        pin["archive_sha256"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()

        self.pin.write_text(json.dumps(pin))

        with self.assertRaisesRegex(ValueError, r"missing_members=\['acquisition_tool/new\.py'\]"):
            updates.import_archive(self.source, self.pin, self.archive)

        self.assert_old_source()
        self.assertEqual([], list(self.root.glob(".acquisition-next-*")))

    def test_unsafe_or_duplicate_archive_members_never_publish(self):
        for (name, kind) in (
            ("../escape", tarfile.REGTYPE),
            ("/absolute", tarfile.REGTYPE),
            ("acquisition_tool/new.py", tarfile.REGTYPE),
            ("linked", tarfile.SYMTYPE),
            ("hardlinked", tarfile.LNKTYPE),
            ("special", tarfile.FIFOTYPE),
            ("acquisition-manifest.json/child", tarfile.REGTYPE),
        ):
            with self.subTest(name=name):
                self.write_archive(extra=((name, kind),))

                with self.assertRaises(ValueError):
                    updates.import_archive(self.source, self.pin, self.archive)

                self.assert_old_source()

    def test_duplicate_archive_member_reports_extra_occurrences(self):
        self.write_archive(extra=(("acquisition_tool/new.py", tarfile.REGTYPE),))

        with self.assertRaises(ValueError) as caught:
            updates.import_archive(self.source, self.pin, self.archive)

        self.assertIn("extra_member_occurrences=Counter({'acquisition_tool/new.py': 1})", str(caught.exception))
        self.assert_old_source()

    def test_git_checkout_modes_preserve_only_the_executable_contract(self):
        path = self.source / "acquisition_tool/old.py"

        path.chmod(0o664)
        updates.verify(self.source)
        path.chmod(0o755)

        with self.assertRaisesRegex(ValueError, "actual_execute_bits=111.*release_execute_bits=000"):
            updates.verify(self.source)

    def test_manifest_rejects_execute_bits_and_accepts_checkout_write_bits(self):
        manifest_path = self.source / "acquisition-manifest.json"

        manifest_path.chmod(0o664)
        updates.verify(self.source)
        manifest_path.chmod(0o654)

        with self.assertRaisesRegex(ValueError, "manifest_execute_bits=010"):
            updates.verify(self.source)

    def test_manifest_rejects_a_hard_link_outside_the_snapshot(self):
        manifest_path = self.source / "acquisition-manifest.json"

        os.link(manifest_path, self.root / "manifest-alias")

        with self.assertRaisesRegex(ValueError, "manifest_link_count=2"):
            updates.verify(self.source)

    def test_payload_rejects_a_hard_link_outside_the_snapshot(self):
        acquisition_file = self.source / "acquisition_tool/old.py"
        acquisition_alias = self.root / "payload-alias"

        os.link(acquisition_file, acquisition_alias)

        with self.assertRaisesRegex(ValueError, "acquisition_rel_path='acquisition_tool/old.py'.*acquisition_link_count=2"):
            updates.verify(self.source)

        acquisition_alias.unlink()
        self.assert_old_source()

    def test_unrecorded_directory_or_symlink_is_not_admitted(self):
        extra = self.source / "extra"

        extra.mkdir()

        with self.assertRaisesRegex(ValueError, "unrecorded acquisition_rel_directory='extra'"):
            updates.verify(self.source)

        extra.rmdir()
        extra.symlink_to(self.root, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "directory is a symlink"):
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
        (primary, cleanup) = (ValueError("candidate stage failed"), PermissionError("stage cleanup failed"))
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

    def test_download_cleanup_failure_reports_published_source(self):
        download_directory = self.root / "download"
        download_directory.mkdir()
        cleanup_failure = PermissionError("download cleanup failed")

        class DownloadDirectory:
            def __enter__(self):
                return str(download_directory)

            def __exit__(self, exception_type, exception, traceback):
                raise cleanup_failure

        with (
            patch.object(updates.tempfile, "TemporaryDirectory", return_value=DownloadDirectory()),
            patch.object(updates.subprocess, "run", side_effect=self.download),
            self.assertRaises(PermissionError) as caught,
        ):
            updates.update(self.source, self.pin)

        self.assertIs(cleanup_failure, caught.exception)
        self.assertIn("published", " ".join(cleanup_failure.__notes__))
        self.assertIn(str(self.source), " ".join(cleanup_failure.__notes__))
        self.assertEqual("2.0.0", updates.verify(self.source)["release_version"])

    def test_edit_during_download_is_not_overwritten(self):
        def download_and_edit(arguments, *, check):
            self.download(arguments, check=check)
            (self.source / "acquisition_tool/old.py").write_bytes(b"edit during download")

        with patch.object(updates.subprocess, "run", side_effect=download_and_edit), self.assertRaises(ValueError):
            updates.update(self.source, self.pin)

        self.assertEqual(b"edit during download", (self.source / "acquisition_tool/old.py").read_bytes())
        self.assertFalse(self.download_directory.exists())
        self.assertEqual([], list(self.root.glob(".acquisition-next-*")))
