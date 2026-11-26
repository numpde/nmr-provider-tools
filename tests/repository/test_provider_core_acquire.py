"""SDK acquisition publishes only pip-verified locked wheels."""

from tools.container_guard import require_container

require_container()

import pathlib
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from sdk_build import provider_core_acquire
from tools.wheelhouse import digest
from tools.adapters.pip_install import hashed_requirement


class ProviderCoreAcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.index = self.root / "index"

        self.index.mkdir()
        self.wheel = self.index / "sample-1.0-py3-none-any.whl"

        with zipfile.ZipFile(self.wheel, "w") as archive:
            prefix = "sample-1.0.dist-info/"

            archive.writestr(prefix + "METADATA", "Metadata-Version: 2.1\n" "Name: sample\n" "Version: 1.0\n")
            archive.writestr(prefix + "WHEEL", "Wheel-Version: 1.0\n" "Root-Is-Purelib: true\n" "Tag: py3-none-any\n")
            archive.writestr(prefix + "RECORD", "")

        self.lock = self.root / "requirements.lock"

        self.lock.write_text(hashed_requirement("sample", "1.0", digest(self.wheel)))
        self.bootstrap = self.root / "pylock.toml"

        self.bootstrap.write_text(
            'lock-version = "1.0"\n' '[[packages]]\n' 'name = "sample"\n' 'version = "1.0"\n'
            '[[packages.wheels]]\n' 'url = "https://example.invalid/' + self.wheel.name + '"\n'
            'hashes = { sha256 = "' + digest(self.wheel) + '" }\n'
        )

    def local_download(self, command, **options):
        """Use the fixture publisher while exercising the real hashed pip download."""

        if "download" in command:
            index = command.index("--index-url")
            command = command[:index] + ["--no-index", "--find-links", str(self.index)] + command[index + 2:]

        return self.run_process(command, **options)

    def acquire(self):
        self.run_process = subprocess.run

        with patch.object(provider_core_acquire, "require_download_proxy"), patch.object(provider_core_acquire.subprocess, "run", side_effect=self.local_download):
            provider_core_acquire.acquire_requirements(self.pool, self.lock)

    def test_acquisition_publishes_exact_wheels_and_reuses_existing_inode(self):
        self.acquire()
        cached_wheel = self.pool / self.wheel.name
        inode = cached_wheel.stat().st_ino

        self.assertEqual(self.wheel.read_bytes(), cached_wheel.read_bytes())
        self.acquire()
        self.assertEqual(inode, cached_wheel.stat().st_ino)
        self.assertEqual([self.wheel.name], [cached_wheel.name for cached_wheel in self.pool.iterdir()])

    def test_conflicting_cached_bytes_are_preserved(self):
        cached_wheel = self.pool / self.wheel.name

        cached_wheel.write_bytes(b"conflicting bytes")

        with self.assertRaises(FileExistsError) as rejected:
            self.acquire()

        self.assertIsInstance(rejected.exception.__cause__, ValueError)
        self.assertIn("existing file was retained", str(rejected.exception.__cause__))

        self.assertEqual(b"conflicting bytes", cached_wheel.read_bytes())
        self.assertEqual([self.wheel.name], [cached_wheel.name for cached_wheel in self.pool.iterdir()])

    def test_download_hash_failure_never_publishes_a_wheel(self):
        self.lock.write_text(hashed_requirement("sample", "1.0", "a" * 64))

        with self.assertRaises(subprocess.CalledProcessError):
            self.acquire()

        self.assertEqual([], list(self.pool.iterdir()))

    def test_offline_check_verifies_requirements_and_bootstrap_without_fetching(self):
        shutil.copyfile(self.wheel, self.pool / self.wheel.name)

        with patch.object(provider_core_acquire, "require_download_proxy", side_effect=AssertionError("offline check fetched bytes")):
            provider_core_acquire.check_environments(self.pool, [self.lock], self.bootstrap)

        self.wheel.write_bytes(b"changed")
        (self.pool / self.wheel.name).write_bytes(b"changed")

        with self.assertRaises(ValueError):
            provider_core_acquire.check_environments(self.pool, [], self.bootstrap)

    def test_missing_bootstrap_wheel_is_fatal(self):
        with self.assertRaises(FileNotFoundError):
            provider_core_acquire.check_environments(self.pool, [], self.bootstrap)

    def test_cached_bootstrap_conflict_cannot_be_replaced(self):
        (artifact,) = provider_core_acquire.bootstrap_wheels(self.bootstrap)

        (self.pool / artifact["name"]).write_bytes(b"changed")

        with self.assertRaisesRegex(ValueError, "existing file was retained"):
            provider_core_acquire.acquire_artifact(self.pool, artifact)

        self.assertEqual(b"changed", (self.pool / artifact["name"]).read_bytes())
