"""The cold source bundle is reproducible and uses only qualified dependency bytes."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
import zipfile

from tools.build_acquisition_bundle import build, INSTALLER_SOURCES, PROXY_FILES, SOURCE_FILES
from tests.repository.quality.sources import ROOT


class AcquisitionBundleTests(unittest.TestCase):
    def test_missing_pinned_identity_wheel_names_its_acquisition_action(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["bash", str(ROOT / "host/tools/acquisition_identity_test.sh")],
                env=os.environ | {
                    "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///fixture.sock",
                    "WHEELHOUSE_INPUT_ORIGIN": "command line",
                    "WHEELHOUSE_INPUT": directory,
                }, capture_output=True, text=True, timeout=10, check=False)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("Cannot test acquisition identity:", result.stderr)
            self.assertIn("is missing from WHEELHOUSE", result.stderr)
            self.assertIn("make packages/provider-core/download", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def fixture(self, root):
        source = root / "source"
        for name in SOURCE_FILES:
            path = source / "host" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# cold source\n")
        (source / "compose").mkdir()
        (source / "compose/acquisition.yml").write_bytes((ROOT / "compose/acquisition.yml").read_bytes())
        for source_name, _issued_name in INSTALLER_SOURCES:
            path = source / source_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / source_name).read_bytes())
        installer = json.loads((source / "requirements/installer-release.json").read_text())
        payload = b"qualified installer fixture"
        (root / installer["name"]).write_bytes(payload)
        installer.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        (source / "requirements/installer-release.json").write_text(json.dumps(installer))
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
            expected = {"compose/acquisition.yml"} | set(SOURCE_FILES) | {"provider_core/" + name for name in PROXY_FILES} | {"linux_idmap/__init__.py"}
            installer = json.loads((source / "requirements/installer-release.json").read_text())
            expected.update(issued_name for _source_name, issued_name in INSTALLER_SOURCES)
            expected.add("acquisition_tool/installer/wheels/" + installer["name"])
            with tarfile.open(first, "r:gz") as archive:
                self.assertEqual(expected, set(archive.getnames()))
                for member in archive.getmembers():
                    self.assertTrue(member.isfile())
                    self.assertEqual((0, 0, "", "", 0o644, 315532800),
                                     (member.uid, member.gid, member.uname, member.gname, member.mode, member.mtime))
                    data = archive.extractfile(member).read()
                    self.assertEqual({"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": 0o644},
                                     record["source_files"][member.name])
            self.assertEqual({identity.name: hashlib.sha256(identity.read_bytes()).hexdigest(),
                              installer["name"]: installer["sha256"]}, record["dependency_inputs"])

    def test_missing_owner_source_or_changed_identity_wheel_never_publishes_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, wheel, identity, pin = self.fixture(root)
            output = root / "bundle.tar.gz"
            (source / "host" / SOURCE_FILES[0]).unlink()
            with self.assertRaises(FileNotFoundError):
                build(root, output, source, wheel, pin)
            self.assertFalse(output.exists())
            (source / "host" / SOURCE_FILES[0]).write_text("# restored\n")
            identity.write_bytes(b"changed dependency")
            with self.assertRaisesRegex(ValueError, "pinned hash"):
                build(root, output, source, wheel, pin)
            self.assertFalse(output.exists())
            self.assertEqual([], list(root.glob("*.part")))
