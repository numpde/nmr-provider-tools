"""The shared delivery release reuses exact wheels without changing the pool."""

from tools.container_guard import require_container

require_container()

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import delivery_image


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
            delivery_image.acquire_release(None, None, self.pool)
        self.assertEqual(inode, self.wheel.stat().st_ino)
        self.assertEqual(self.content, self.wheel.read_bytes())

    def test_conflicting_cached_wheel_is_preserved(self):
        self.wheel.write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "existing file retained"):
            delivery_image.acquire_release(None, None, self.pool)
        self.assertEqual(b"different", self.wheel.read_bytes())

    def test_missing_release_is_downloaded_and_published(self):
        with patch.object(delivery_image, "require_download_proxy"), patch.object(
            delivery_image.urllib.request, "urlopen", return_value=io.BytesIO(self.content)
        ):
            delivery_image.acquire_release(None, None, self.pool)
        self.assertEqual(self.content, self.wheel.read_bytes())

    def test_bad_download_is_not_published(self):
        with patch.object(delivery_image, "require_download_proxy"), patch.object(
            delivery_image.urllib.request, "urlopen", return_value=io.BytesIO(b"different")
        ):
            with self.assertRaisesRegex(ValueError, "downloaded bytes"):
                delivery_image.acquire_release(None, None, self.pool)
        self.assertFalse(self.wheel.exists())
        self.assertEqual([], list(self.pool.iterdir()))
