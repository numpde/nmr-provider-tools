"""The cold source bundle is reproducible and uses only qualified dependency bytes."""

from tools.container_guard import require_container

require_container()

import hashlib
import tarfile
import tempfile
import unittest
from pathlib import Path
import zipfile

from tools.build_acquisition_bundle import build, PROXY_FILES, SOURCE_FILES, source_path


class AcquisitionBundleTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "source"
        for name in SOURCE_FILES:
            path = source / source_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# cold source\n")
        wheel = root / "provider_core-0.6.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name in PROXY_FILES:
                archive.writestr("provider_core/" + name, "# SDK source\n")
        identity = root / "linux_idmap-0.3.0-py3-none-any.whl"
        with zipfile.ZipFile(identity, "w") as archive:
            archive.writestr("linux_idmap/__init__.py", "# qualified identity\n")
        pin = root / "pylock.toml"
        pin.write_text('lock-version = "1.0"\n[[packages]]\nname = "linux-idmap"\nversion = "0.3.0"\n'
                       '[[packages.wheels]]\nurl = "https://example.invalid/' + identity.name + '"\n'
                       'hashes = {sha256 = "' + hashlib.sha256(identity.read_bytes()).hexdigest() + '"}\n')
        return source, wheel, identity, pin

    def test_rebuild_has_identical_bytes_and_normalized_source_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, wheel, identity, pin = self.fixture(root)
            first, second = root / "first.tar.gz", root / "second.tar.gz"
            record = build(root, first, source, wheel, pin)
            self.assertEqual(record, build(root, second, source, wheel, pin))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            expected = set(SOURCE_FILES) | {"provider_core/" + name for name in PROXY_FILES} | {"linux_idmap/__init__.py"}
            with tarfile.open(first, "r:gz") as archive:
                self.assertEqual(expected, set(archive.getnames()))
                for member in archive.getmembers():
                    self.assertTrue(member.isfile())
                    self.assertEqual((0, 0, "", "", 0o644, 315532800),
                                     (member.uid, member.gid, member.uname, member.gname, member.mode, member.mtime))
                    data = archive.extractfile(member).read()
                    self.assertEqual({"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": 0o644},
                                     record["source_files"][member.name])
            self.assertEqual({identity.name: hashlib.sha256(identity.read_bytes()).hexdigest()}, record["dependency_inputs"])

    def test_missing_owner_source_or_changed_identity_wheel_never_publishes_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, wheel, identity, pin = self.fixture(root)
            output = root / "bundle.tar.gz"
            (source / source_path(SOURCE_FILES[0])).unlink()
            with self.assertRaises(FileNotFoundError):
                build(root, output, source, wheel, pin)
            self.assertFalse(output.exists())
            (source / SOURCE_FILES[0]).write_text("# restored\n")
            identity.write_bytes(b"changed dependency")
            with self.assertRaisesRegex(ValueError, "pinned hash"):
                build(root, output, source, wheel, pin)
            self.assertFalse(output.exists())
            self.assertEqual([], list(root.glob("*.part")))
