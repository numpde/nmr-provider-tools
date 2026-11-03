"""The shared delivery release reuses exact wheels without changing the pool."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import core_environment, delivery_image


class DeliveryReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.pool = Path(temporary.name)
        self.wheel = self.pool / "provider_core-1.2.3-py3-none-any.whl"
        self.content = b"released wheel"
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.release = {"version": "1.2.3", "wheel_sha256": self.digest, "runtime_wheels": {}}
        self.inputs = patch.object(delivery_image, "release_inputs", return_value=(self.release, b"lock"))
        self.inputs.start()
        self.addCleanup(self.inputs.stop)

    def test_warm_pool_needs_no_network_and_retains_file(self):
        self.wheel.write_bytes(self.content)
        inode = self.wheel.stat().st_ino
        with patch.object(delivery_image, "require_download_proxy", side_effect=AssertionError("network not needed")):
            delivery_image.acquire_release(None, None, self.pool, Path("unused.pyz"))
        self.assertEqual(inode, self.wheel.stat().st_ino)
        self.assertEqual(self.content, self.wheel.read_bytes())

    def test_conflicting_cached_wheel_is_preserved(self):
        self.wheel.write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "existing file retained"):
            delivery_image.acquire_release(None, None, self.pool, Path("unused.pyz"))
        self.assertEqual(b"different", self.wheel.read_bytes())

    def test_missing_release_is_passed_to_shared_publisher(self):
        with patch.object(delivery_image, "require_download_proxy"), patch.object(
            delivery_image, "publish_release_wheels"
        ) as publish:
            delivery_image.acquire_release(None, None, self.pool, Path("tool.pyz"))
        self.assertEqual(self.wheel.name, publish.call_args.args[2][0][0])
        self.assertEqual(self.digest, publish.call_args.args[2][0][1])

    def test_publisher_failure_is_reported(self):
        with patch.object(delivery_image, "require_download_proxy"), patch.object(
            delivery_image, "publish_release_wheels", side_effect=ValueError("downloaded bytes differ")
        ):
            with self.assertRaisesRegex(ValueError, "downloaded bytes"):
                delivery_image.acquire_release(None, None, self.pool, Path("tool.pyz"))
        self.assertFalse(self.wheel.exists())
        self.assertEqual([], list(self.pool.iterdir()))

    def test_capture_records_pins_without_copying_wheels(self):
        selected = {"lock_sha256": hashlib.sha256(b"lock").hexdigest(),
                    "files": {self.wheel.name: self.digest}}
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            dockerfile = destination / "containers/delivery/Dockerfile"
            dockerfile.parent.mkdir(parents=True)
            dockerfile.write_text("FROM scratch AS wheel_inputs\n# selected-wheel-inputs\n")
            self.wheel.write_bytes(self.content)
            inode = self.wheel.stat().st_ino
            with patch.object(delivery_image, "requirements"), patch.object(
                delivery_image, "missing_wheels", return_value=[]
            ), patch.object(delivery_image, "select_wheels", return_value=selected):
                delivery_image.capture_image_inputs(None, None, self.pool, destination)
            self.assertEqual(
                {"provider-core-install.lock", "provider-core-wheel-inputs.json", "provider-core-stream.json", "containers"},
                {path.name for path in destination.iterdir()},
            )
            self.assertEqual(b"lock", (destination / "provider-core-install.lock").read_bytes())
            self.assertEqual(selected, json.loads((destination / "provider-core-wheel-inputs.json").read_text()))
            self.assertEqual([{"directory": "wheelhouse", "filename": self.wheel.name, "sha256": self.digest}],
                             json.loads((destination / "provider-core-stream.json").read_text()))
            self.assertIn(self.wheel.name, (dockerfile.parent / "Dockerfile.selected").read_text())
            self.assertEqual(inode, self.wheel.stat().st_ino)

    def test_install_uses_pool_directly_and_checks_captured_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            root.mkdir()
            lock = Path(temporary) / "provider-core-install.lock"
            lock.write_bytes(b"lock")
            selected = {"lock_sha256": hashlib.sha256(b"lock").hexdigest(),
                        "files": {self.wheel.name: self.digest}}
            with patch.object(core_environment, "select_wheels", return_value=selected), patch.object(
                core_environment, "install_locked", return_value=selected
            ) as install, patch.object(core_environment.subprocess, "run"):
                core_environment.install_environment(root, lock, self.pool, selected)
            self.assertEqual(self.pool, install.call_args.args[2])
            self.assertEqual(b"lock", (root / "provider-core-install.lock").read_bytes())
            self.assertFalse((root / "wheels").exists())
