"""The issued installer works without ambient pip and preserves caller authority."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.repository.quality.sources import ROOT


class InstallerDriverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installer = self.root / "installer"
        self.installer.mkdir()
        shutil.copyfile(ROOT / "tools/installer.py", self.installer / "driver.py")
        shutil.copyfile(ROOT / "requirements/installer-release.json", self.installer / "release.json")
        self.driver = self.installer / "driver.py"
        self.record = json.loads((self.installer / "release.json").read_text())
        self.wheel = Path("/opt/provider-installer") / self.record["name"]

    def command(self, python, *arguments):
        return [str(python), "-I", "-B", str(self.driver), "--wheel", str(self.wheel), *arguments]

    def test_real_backend_build_and_locked_install_keep_the_target_pip_free(self):
        environment = self.root / "target"
        subprocess.run(["python3", "-I", "-B", "-m", "venv", "--without-pip", str(environment)], check=True)
        python = environment / "bin/python"
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
        subprocess.run(self.command(python, "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
                                    "--wheel-dir", str(output), str(source)), check=True, capture_output=True, text=True)
        wheel, = output.glob("*.whl")
        lock = self.root / "requirements.lock"
        lock.write_text("installer-fixture==1.0 --hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest() + "\n")
        subprocess.run(self.command(python, "pip", "install", "--no-index", "--no-deps", "--require-hashes",
                                    "--find-links", str(output), "-r", str(lock)), check=True, capture_output=True, text=True)
        result = subprocess.run([str(python), "-I", "-B", "-c",
                                 "import importlib.util, installer_fixture; assert installer_fixture.VALUE == 42; assert importlib.util.find_spec('pip') is None"],
                                check=True, capture_output=True, text=True)
        self.assertEqual("", result.stderr)

    def test_module_keeps_the_borrowed_descriptor_and_uses_verified_pip(self):
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        module = self.root / "witness.py"
        module.write_text('''import json
import os
import pip
import sys
print(json.dumps({"version": pip.__version__, "wheel": pip.__file__, "lock_inode": os.fstat(int(sys.argv[1])).st_ino}))
''')
        result = subprocess.run(
            ["python3", "-B", str(self.driver), "--wheel", str(self.wheel), "module", "witness", str(descriptor)],
            env=os.environ | {"PYTHONPATH": str(self.root)}, pass_fds=(descriptor,),
            check=True, capture_output=True, text=True,
        )
        observed = json.loads(result.stdout)
        self.assertEqual(self.record["name"].split("-")[1], observed["version"])
        self.assertIn("provider-installer-", observed["wheel"])
        self.assertEqual(os.fstat(descriptor).st_ino, observed["lock_inode"])

    def test_bad_wheel_inputs_fail_before_module_execution(self):
        rejected = self.root / "rejected.whl"
        for data in (b"", b"damaged", self.wheel.read_bytes() + b"extra"):
            with self.subTest(size=len(data)):
                rejected.write_bytes(data)
                result = subprocess.run(
                    ["python3", "-I", "-B", str(self.driver), "--wheel", str(rejected), "module", "module_must_not_run"],
                    check=False, capture_output=True, text=True,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn("wheel size or SHA-256 differs", result.stderr)
                self.assertNotIn("No module named", result.stderr)
        rejected.unlink()
        rejected.symlink_to(self.wheel)
        result = subprocess.run(["python3", "-I", "-B", str(self.driver), "--wheel", str(rejected), "pip", "--version"],
                                check=False, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Too many levels of symbolic links", result.stderr)

    def test_bad_release_path_is_rejected_before_opening_the_wheel(self):
        self.record["name"] = "../outside.whl"
        (self.installer / "release.json").write_text(json.dumps(self.record))
        result = subprocess.run(self.command("python3", "pip", "--version"), check=False, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("release record must identify one pip wheel", result.stderr)
