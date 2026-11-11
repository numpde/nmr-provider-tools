"""Image-owned pip seeds environments that build and install locked inputs."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.repository.quality.sources import ROOT
from tools import installer
from tools.build_acquisition_bundle import INSTALLER_SOURCES
from tools.installer import pip_command


class InstallerImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.record = installer.release(ROOT / "requirements/installer-release.json")

    def test_real_backend_build_and_locked_install_use_the_target_installer(self):
        environment = self.root / "target"
        python = installer.create_environment(environment)
        source = self.root / "source"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
        )
        (source / "backend.py").write_text('''import pathlib
import zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "installer_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w") as wheel:
        wheel.writestr("installer_fixture.py", "VALUE = 42\\n")
        wheel.writestr("installer_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\\nName: installer-fixture\\nVersion: 1.0\\n")
        wheel.writestr("installer_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")
        wheel.writestr("installer_fixture-1.0.dist-info/RECORD", "")
    return name
''')
        output = self.root / "wheels"
        output.mkdir()
        command = pip_command(python)
        subprocess.run([*command, "wheel", "--no-index", "--no-deps", "--no-build-isolation",
                        "--wheel-dir", str(output), str(source)], check=True, capture_output=True, text=True)
        wheel, = output.glob("*.whl")
        lock = self.root / "requirements.lock"
        lock.write_text("installer-fixture==1.0 --hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest() + "\n")
        subprocess.run([*command, "install", "--no-index", "--no-deps", "--require-hashes",
                        "--find-links", str(output), "-r", str(lock)], check=True, capture_output=True, text=True)
        result = subprocess.run([str(python), "-I", "-B", "-c",
                                 "import json, pip, sys, installer_fixture; "
                                 "assert installer_fixture.VALUE == 42; "
                                 "print(json.dumps([pip.__version__, pip.__file__, sys.prefix]))"],
                                check=True, capture_output=True, text=True)
        version, installed, prefix = json.loads(result.stdout)
        self.assertEqual(self.record["name"].split("-")[1], version)
        self.assertEqual(environment, Path(prefix))
        self.assertTrue(Path(installed).is_relative_to(environment))

    def test_issued_helper_uses_its_guard_with_a_conflicting_consumer_tools_package(self):
        python = installer.create_environment(self.root / "consumer")
        site = Path(subprocess.check_output(
            [str(python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
        ).strip())
        for source, issued in INSTALLER_SOURCES:
            if source.endswith(".py"):
                destination = site / issued
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((ROOT / source).read_bytes())
        (site / "tools").mkdir()
        (site / "tools/__init__.py").write_text("raise AssertionError('consumer tools must not supply the SDK guard')\n")
        command = [str(python), "-I", "-B", "-c",
                   "from acquisition_tool.installer.tools.installer import create_environment"]
        environment = os.environ | {"NMR_PROVIDER_TOOLS_IN_CONTAINER": "1"}
        subprocess.run(command, env=environment, check=True, capture_output=True, text=True)
        del environment["NMR_PROVIDER_TOOLS_IN_CONTAINER"]
        refused = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("Cannot run repository Python without its container launcher", refused.stderr)

    def test_bad_wheel_bytes_or_symlinks_never_start_installation(self):
        wheel = self.root / self.record["name"]
        for data in (b"", b"damaged", b"x" * (self.record["size"] + 1)):
            with self.subTest(size=len(data)), patch("tools.installer.subprocess.run") as dispatch:
                wheel.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "wheel size or SHA-256 differs"):
                    installer.install(self.root, self.record)
                dispatch.assert_not_called()
        wheel.unlink()
        wheel.symlink_to(ROOT / "requirements/installer-release.json")
        with patch("tools.installer.subprocess.run") as dispatch:
            with self.assertRaises(OSError):
                installer.install(self.root, self.record)
            dispatch.assert_not_called()

    def test_capture_does_not_rebind_after_pool_replacement(self):
        data = b"verified fixture wheel"
        record = self.record | {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        wheel = self.root / record["name"]
        wheel.write_bytes(data)
        captured = self.root / "captured.whl"
        installer.capture(wheel, record, captured)
        wheel.unlink()
        wheel.write_bytes(b"replacement")
        self.assertEqual(data, captured.read_bytes())

    def test_release_rejects_escaping_wheel_names_and_duplicate_fields(self):
        path = self.root / "release.json"
        path.write_text(json.dumps(self.record | {"name": "../outside.whl"}))
        with self.assertRaisesRegex(ValueError, "release record must identify one pip wheel"):
            installer.release(path)
        path.write_text('{"name":"first","name":"second"}')
        with self.assertRaisesRegex(ValueError, "duplicate field"):
            installer.release(path)
