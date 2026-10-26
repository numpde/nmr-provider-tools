"""Release qualification refuses omitted source and unsafe archive membership."""

from tools.container_guard import require_container

require_container()

import io
from contextlib import redirect_stdout
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.package import artifact_names, check, source_payload, unpack_source, wheel_payload
from tools.wheelhouse import digest


class PackageArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_source_archive_refuses_traversal_links_duplicate_and_special_members_before_extraction(self):
        for name, kind in (("provider_core-0.2.0/../../outside", tarfile.REGTYPE),
                           ("/absolute", tarfile.REGTYPE), ("provider_core-0.2.0/link", tarfile.SYMTYPE),
                           ("provider_core-0.2.0/device", tarfile.CHRTYPE), ("provider_core-0.2.0/duplicate", "duplicate")):
            with self.subTest(name=name, kind=kind):
                path = self.root / "source.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    member.mode = 0o644
                    if kind == "duplicate":
                        archive.addfile(member, io.BytesIO())
                    else:
                        member.type = kind
                    archive.addfile(member, io.BytesIO())
                destination = self.root / "extracted"
                with self.assertRaisesRegex(ValueError, "source archive"):
                    unpack_source(path, destination, "0.2.0")
                self.assertFalse(destination.exists())

    def test_source_inventory_detects_changed_bytes_and_omitted_test_files(self):
        source = self.root / "source"
        (source / "src/provider_core").mkdir(parents=True)
        (source / "tests").mkdir()
        for name in ("README.md", "MANIFEST.in", "pyproject.toml", "src/provider_core/__init__.py", "tests/test_contract.py"):
            (source / name).write_text("authored\n")
        inventory = {path.relative_to(source).as_posix(): {"sha256": digest(path), "size": path.stat().st_size,
                     "mode": path.stat().st_mode & 0o777} for path in source.rglob("*") if path.is_file()}
        metadata = source / "src/provider_core.egg-info"
        metadata.mkdir()
        for name in ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "requires.txt", "top_level.txt"):
            (metadata / name).write_text("")
        for name in ("PKG-INFO", "setup.cfg"):
            (source / name).write_text("")
        self.assertEqual({"provider_core/__init__.py": b"authored\n"}, source_payload(source, inventory))
        (source / "tests/test_contract.py").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "pre-build source inventory"):
            source_payload(source, inventory)
        (source / "tests/test_contract.py").unlink()
        with self.assertRaisesRegex(ValueError, "pre-build source inventory"):
            source_payload(source, inventory)

    def test_wheel_payload_detects_missing_extra_or_changed_runtime_files(self):
        path = self.root / "core.whl"
        expected = {"provider_core/__init__.py": b"owned\n", "provider_core/delivery_client.py": b"client\n"}
        for payload in ({"provider_core/__init__.py": b"owned\n"}, expected | {"tools/helper.py": b"leak"},
                        expected | {"provider_core/delivery_client.py": b"changed"}):
            with self.subTest(payload=payload):
                with zipfile.ZipFile(path, "w") as archive:
                    for name, value in payload.items():
                        archive.writestr(name, value)
                with self.assertRaisesRegex(ValueError, "wheel"):
                    wheel_payload(path, expected, "0.2.0")

    def test_corrupt_release_manifest_refuses_before_rebuild_and_emits_no_qualification_receipt(self):
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        for name in artifact_names("0.2.0"):
            (artifacts / name).write_text("{}\n")
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(ValueError, "release hashes"):
            check(artifacts, "0" * 40)
        self.assertEqual("", output.getvalue())
