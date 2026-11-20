"""The cold source bundle is reproducible and uses only qualified dependency bytes."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import pathlib
import zipfile

from build_acquisition_bundle import build, INSTALLER_SOURCES, PROXY_FILES, SOURCE_FILES
from acquisition_tool.source_update import verify
from tests.repository.quality.sources import ROOT


class AcquisitionBundleTests(unittest.TestCase):
    def test_missing_pinned_identity_wheel_names_its_acquisition_action(self):
        with tempfile.TemporaryDirectory() as directory:
            completed_process = subprocess.run(
                ["bash", str(ROOT / "host/tools/acquisition_identity_test.sh")],
                env=os.environ | {
                    "NMR_SELECTED_DOCKER_ENDPOINT": "unix:///fixture.sock",
                    "WHEELHOUSE_INPUT_ORIGIN": "command line",
                    "WHEELHOUSE_INPUT": directory,
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False
            )

            self.assertNotEqual(0, completed_process.returncode)
            self.assertEqual("", completed_process.stdout)
            self.assertIn("Cannot test acquisition identity:", completed_process.stderr)
            self.assertIn("is missing from WHEELHOUSE", completed_process.stderr)
            self.assertIn("make packages/provider-core/download", completed_process.stderr)
            self.assertNotIn("Traceback", completed_process.stderr)

    def fixture(self, root):
        source = root / "source"

        for name in SOURCE_FILES:
            staged_source_path = source / "host" / name

            if name == "acquisition_tool/validation/upstream/container_ids.sh":
                staged_source_path = source / "operations/docker/validation/upstream/container_ids.sh"
            elif name.startswith("acquisition_tool/"):
                staged_source_path = source / "packages/acquisition" / name
            elif name == "tools/docker_client.sh":
                staged_source_path = source / "operations/docker/docker_client.sh"

            staged_source_path.parent.mkdir(parents=True, exist_ok=True)
            staged_source_path.write_bytes((ROOT / staged_source_path.relative_to(source)).read_bytes())

        (source / "packages/acquisition/compose").mkdir(parents=True)
        (source / "packages/acquisition/compose/acquisition.yml").write_bytes((ROOT / "packages/acquisition/compose/acquisition.yml").read_bytes())

        for (source_name, _issued_name) in INSTALLER_SOURCES:
            staged_source_path = source / source_name

            staged_source_path.parent.mkdir(parents=True, exist_ok=True)
            staged_source_path.write_bytes((ROOT / source_name).read_bytes())

        installer = json.loads((source / "requirements/installer-release.json").read_text())
        payload = b"qualified installer fixture"

        (root / installer["name"]).write_bytes(payload)
        installer.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        (source / "requirements/installer-release.json").write_text(json.dumps(installer))
        wheel = root / "provider_core-0.6.0-py3-none-any.whl"

        with zipfile.ZipFile(wheel, "w") as archive:
            for name in PROXY_FILES:
                archive.writestr("provider_core/" + name, (ROOT / "packages/provider-core/src/provider_core" / name).read_bytes())

            archive.writestr("provider_core/type_requirements.py", (ROOT / "packages/provider-core/src/provider_core/type_requirements.py").read_bytes())
            archive.writestr("provider_core/_type_requirements.py", (ROOT / "packages/type-requirements/src/type_requirements.py").read_bytes())

        identity = root / "linux_idmap-0.3.0-py3-none-any.whl"

        with zipfile.ZipFile(identity, "w") as archive:
            archive.writestr("linux_idmap/__init__.py", "# qualified identity\n")

        pin = root / "pylock.toml"

        pin.write_text(
            'lock-version = "1.0"\n'
            '[[packages]]\n'
            'name = "linux-idmap"\n'
            'version = "0.3.0"\n'
            '[[packages.wheels]]\n'
            'url = "https://example.invalid/' + identity.name + '"\n'
            'hashes = {sha256 = "' + hashlib.sha256(identity.read_bytes()).hexdigest() + '"}\n'
        )

        (root / "wheelhouse-tool-0.5.9.pyz").write_bytes(b"selected built wheel tool")

        return (source, wheel, identity, pin)

    def build(self, root, output, source, wheel, pin):
        return build(
            root,
            output,
            source,
            wheel,
            pin,
            revision="a" * 40,
            wheelhouse_bundle=root / "wheelhouse-tool-0.5.9.pyz"
        )

    def test_rebuild_has_identical_bytes_and_normalized_source_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (source, wheel, identity, pin) = self.fixture(root)
            (first, second) = (root / "first.tar.gz", root / "second.tar.gz")
            record = self.build(root, first, source, wheel, pin)

            self.assertEqual(record, self.build(root, second, source, wheel, pin))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            expected = {"compose/acquisition.yml", "acquisition-manifest.json"} | set(SOURCE_FILES) | {("provider_core/" + name) for name in PROXY_FILES} | {"linux_idmap/__init__.py"}

            expected.update({"provider_core/type_requirements.py", "provider_core/_type_requirements.py"})
            installer = json.loads((source / "requirements/installer-release.json").read_text())

            expected.update(issued_name for (_source_name, issued_name) in INSTALLER_SOURCES)
            expected.add("acquisition_tool/installer/wheels/" + installer["name"])

            with tarfile.open(first, "r:gz") as archive:
                self.assertEqual(expected, set(archive.getnames()))
                manifest = json.load(archive.extractfile("acquisition-manifest.json"))

                self.assertEqual("0.6.0", manifest["release_version"])
                self.assertEqual("a" * 40, manifest["source_revision"])
                self.assertEqual(expected - {"acquisition-manifest.json"}, set(manifest["payload_files"]))
                self.assertEqual(record["dependency_inputs"], manifest["dependency_inputs"])

                for member in archive.getmembers():
                    self.assertTrue(member.isfile())

                    self.assertEqual(
                        (0, 0, "", "", 0o644, 315532800),
                        (member.uid, member.gid, member.uname, member.gname, member.mode, member.mtime)
                    )

                    archive_member_bytes = archive.extractfile(member).read()

                    self.assertEqual(
                        {"sha256": hashlib.sha256(archive_member_bytes).hexdigest(), "size": len(archive_member_bytes), "mode": 0o644},
                        record["source_files"][member.name]
                    )

            self.assertEqual(
                {
                    identity.name: hashlib.sha256(identity.read_bytes()).hexdigest(),
                    installer["name"]: installer["sha256"],
                    "wheelhouse-tool-0.5.9.pyz": hashlib.sha256(b"selected built wheel tool").hexdigest()
                },
                record["dependency_inputs"]
            )

    def test_bundled_entrypoints_run_without_the_installed_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (source, wheel, _identity, pin) = self.fixture(root)
            archive_path = root / "bundle.tar.gz"

            self.build(root, archive_path, source, wheel, pin)
            extracted = root / "extracted"

            extracted.mkdir()

            with tarfile.open(archive_path) as archive:
                archive.extractall(extracted, filter="data")

            completed_process = subprocess.run(
                [
                    sys.executable,  # Use this contained interpreter
                    "-I",  # Ignore host Python configuration
                    "-B",  # Leave extracted sources unchanged
                    "-c",  # Launch the extracted package with no checkout path
                    "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
                    "runpy.run_module('provider_core.bound_http_proxy', run_name='__main__')",
                    str(extracted),  # Select the cold bundle's regular package
                    "--help",  # Import the CLI without opening sockets
                ],
                cwd=extracted,
                env=os.environ | {"PROVIDER_CORE_IN_CONTAINER": "1"},
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )

            self.assertIn("--relay-socket", completed_process.stdout)

            resolver_help = subprocess.run(
                [
                    sys.executable,  # Use this contained interpreter
                    "-I",  # Ignore host Python configuration
                    "-S",  # Exclude installed site packages
                    "-B",  # Leave extracted sources unchanged
                    str(extracted / "acquisition_tool/dns.py"),  # Use the issued resolver entrypoint
                    "--help",  # Load the resolver code without capturing host settings
                ],
                cwd=extracted,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )

            self.assertIn("--interface", resolver_help.stdout)

            update_help = subprocess.run(
                [
                    "bash",  # Run the issued maintenance launcher
                    str(extracted / "acquisition_tool/maintain_source.sh"),  # Use the extracted source package
                    "update",  # Select bootstrap downloading
                    "--help",  # Import the downloader without starting a transfer
                ],
                cwd=extracted,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )

            self.assertIn("--pin-file", update_help.stdout)

    def test_legacy_proxy_without_type_helpers_keeps_its_wheel_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (source, wheel, _identity, pin) = self.fixture(root)
            legacy_wheel = root / "legacy" / wheel.name
            legacy_proxy = b'"""Legacy proxy fixture without type helper imports."""\n'
            helper_members = {"provider_core/type_requirements.py", "provider_core/_type_requirements.py"}

            legacy_wheel.parent.mkdir()

            with zipfile.ZipFile(wheel) as current, zipfile.ZipFile(legacy_wheel, "w") as legacy:
                for member in current.namelist():
                    if member in helper_members:
                        continue

                    payload = legacy_proxy if member == "provider_core/bound_http_proxy.py" else current.read(member)

                    legacy.writestr(member, payload)

            output = root / "legacy.tar.gz"

            self.build(root, output, source, legacy_wheel, pin)

            with tarfile.open(output) as archive:
                self.assertEqual(legacy_proxy, archive.extractfile("provider_core/bound_http_proxy.py").read())
                self.assertTrue(helper_members.isdisjoint(archive.getnames()))

    def test_missing_proxy_type_helpers_never_publish_bundle(self):
        for missing_helpers in (
            {"provider_core/type_requirements.py"},
            {"provider_core/_type_requirements.py"},
            {"provider_core/type_requirements.py", "provider_core/_type_requirements.py"},
        ):
            with self.subTest(missing_helpers=missing_helpers), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                (source, wheel, _identity, pin) = self.fixture(root)
                incomplete_wheel = root / "incomplete" / wheel.name

                incomplete_wheel.parent.mkdir()

                with zipfile.ZipFile(wheel) as current, zipfile.ZipFile(incomplete_wheel, "w") as incomplete:
                    for member in current.namelist():
                        if member not in missing_helpers:
                            incomplete.writestr(member, current.read(member))

                output = root / "incomplete.tar.gz"

                with self.assertRaises(ValueError):
                    self.build(root, output, source, incomplete_wheel, pin)

                self.assertFalse(output.exists())

    def test_missing_owner_source_or_changed_identity_wheel_never_publishes_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (source, wheel, identity, pin) = self.fixture(root)
            output = root / "bundle.tar.gz"

            (source / "packages/acquisition" / SOURCE_FILES[0]).unlink()

            with self.assertRaises(FileNotFoundError):
                self.build(root, output, source, wheel, pin)

            self.assertFalse(output.exists())
            (source / "packages/acquisition" / SOURCE_FILES[0]).write_text("# restored\n")
            identity.write_bytes(b"changed dependency")

            with self.assertRaisesRegex(ValueError, r"wheel_sha256=.* should match expected_sha256="):
                self.build(root, output, source, wheel, pin)

            self.assertFalse(output.exists())
            self.assertEqual([], list(root.glob("*.part")))

    def test_dependency_payload_addition_needs_no_consumer_inventory_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (source, wheel, identity, pin) = self.fixture(root)

            with zipfile.ZipFile(identity, "a") as dependency:
                dependency.writestr("linux_idmap/ordinary_new_module.py", b"VALUE = 1\n")

            old_pin = pin.read_text()

            pin.write_text(
                re.sub(
                    r'sha256 = "[0-9a-f]{64}"',
                    'sha256 = "' + hashlib.sha256(identity.read_bytes()).hexdigest() + '"',
                    old_pin
                )
            )

            archive_path = root / "bundle.tar.gz"

            self.build(root, archive_path, source, wheel, pin)
            extracted = root / "extracted"

            extracted.mkdir()

            with tarfile.open(archive_path) as archive:
                archive.extractall(extracted, filter="data")

            manifest = verify(extracted)

            self.assertIn("linux_idmap/ordinary_new_module.py", manifest["payload_files"])

    def test_current_bootstrap_imports_a_new_bundle_with_the_authored_updater(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (source, wheel, _identity, pin) = self.fixture(root)
            authored_updater = ROOT / "packages/acquisition/acquisition_tool/source_update.py"

            (source / "packages/acquisition/acquisition_tool/source_update.py").write_bytes(authored_updater.read_bytes())
            archive_path = root / "candidate.tar.gz"

            self.build(root, archive_path, source, wheel, pin)

            release_pin = root / "release-pin.json"

            release_pin.write_text(
                json.dumps(
                    {
                        "version": "0.6.0",
                        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                    }
                )
            )

            snapshot = root / "snapshot"

            shutil.copytree(ROOT / "bootstrap/acquisition", snapshot)

            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(ROOT / "bootstrap/acquisition/acquisition_tool/source_update.py"),
                    "import",
                    "--source-dir",
                    str(snapshot),
                    "--pin-file",
                    str(release_pin),
                    "--archive",
                    str(archive_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(authored_updater.read_bytes(), (snapshot / "acquisition_tool/source_update.py").read_bytes())
            self.assertEqual("0.6.0", verify(snapshot)["release_version"])

            subprocess.run(
                [
                    sys.executable,  # Use the contained interpreter
                    "-I",  # Exclude ambient Python paths
                    "-S",  # Prove the bundle needs no installed dependencies
                    "-B",  # Preserve the authenticated snapshot
                    str(snapshot / "acquisition_tool/source_update.py"),  # Run the newly imported updater directly
                    "verify",  # Verify its complete installed source closure
                    "--source-dir",  # Select the authenticated bundle root
                    str(snapshot),  # Include the bundled type helper in verification
                ],
                check=True,
                capture_output=True,
                text=True,
            )
