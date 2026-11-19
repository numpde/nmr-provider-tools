"""The shared delivery release reuses exact wheels without changing the pool."""

from tools.container_guard import require_container

require_container()

import hashlib
import io
import json
import tempfile
import unittest
import pathlib
from unittest.mock import patch

from operations.delivery import build_inputs as delivery_image
from sdk_build import core_environment
from tests.repository.quality.sources import ROOT


class DeliveryReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)
        self.pool = pathlib.Path(temporary.name)
        self.wheel = self.pool / "provider_core-1.2.3-py3-none-any.whl"
        self.content = b"released wheel"
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.release = {"version": "1.2.3", "wheel_sha256": self.digest, "runtime_wheels": {}}
        self.inputs = patch.object(delivery_image, "release_inputs", return_value=(self.release, b"lock"))

        self.inputs.start()
        self.addCleanup(self.inputs.stop)

    def test_install_lock_mismatch_reports_both_hashes(self):
        self.inputs.stop()
        runtime_lock = self.pool / "runtime.lock"

        runtime_lock.write_bytes(b"")
        pin = self.pool / "release.json"
        expected_install_lock_sha256 = "0" * 64

        pin.write_text(json.dumps(self.release | {"install_lock_sha256": expected_install_lock_sha256}))

        install_lock_bytes = (
            f"\n"
            f"provider-core==1.2.3 \\\n"
            f"    --hash=sha256:{self.digest}\n"
        ).encode()

        install_lock_sha256 = hashlib.sha256(install_lock_bytes).hexdigest()

        with self.assertRaises(ValueError) as rejected:
            delivery_image.release_inputs(pin, runtime_lock)

        self.assertEqual(
            first=f"{install_lock_sha256=} should match {expected_install_lock_sha256=}",
            second=str(rejected.exception),
        )

    def test_warm_pool_needs_no_network_and_retains_file(self):
        self.wheel.write_bytes(self.content)
        inode = self.wheel.stat().st_ino

        with patch.object(delivery_image, "require_download_proxy", side_effect=AssertionError("network not needed")):
            delivery_image.acquire_release(None, None, self.pool, pathlib.Path("unused.pyz"))

        self.assertEqual(inode, self.wheel.stat().st_ino)
        self.assertEqual(self.content, self.wheel.read_bytes())

    def test_conflicting_cached_wheel_is_preserved(self):
        self.wheel.write_bytes(b"different")

        with self.assertRaisesRegex(ValueError, r"wheel_sha256=.* should match expected_sha256=.*\. The existing file was retained"):
            delivery_image.acquire_release(None, None, self.pool, pathlib.Path("unused.pyz"))

        self.assertEqual(b"different", self.wheel.read_bytes())

    def test_missing_release_is_passed_to_shared_publisher(self):
        with patch.object(delivery_image, "require_download_proxy"), patch.object(
            delivery_image,
            "publish_release_wheels"
        ) as publish:
            delivery_image.acquire_release(None, None, self.pool, pathlib.Path("tool.pyz"))

        self.assertEqual(self.wheel.name, publish.call_args.args[2][0][0])
        self.assertEqual(self.digest, publish.call_args.args[2][0][1])

    def test_runtime_pin_selects_only_its_pypi_file_before_publishing(self):
        runtime_wheel = "example-2.0-py3-none-any.whl"
        runtime_sha256 = "a" * 64
        runtime_url = "https://files.pythonhosted.org/packages/" + runtime_wheel
        self.release["runtime_wheels"] = {runtime_wheel: runtime_sha256}

        metadata = {
            "urls": [
                {"filename": "unrelated-1.0-py3-none-any.whl"},
                {"filename": runtime_wheel, "digests": {"sha256": "b" * 64}},
                {"filename": runtime_wheel, "digests": {"sha256": runtime_sha256}, "url": runtime_url},
            ],
        }

        with (
            patch.object(delivery_image, "require_download_proxy"),
            patch("tools.adapters.pypi_project_json.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(metadata).encode())) as request,
            patch.object(delivery_image, "publish_release_wheels") as publish,
        ):
            delivery_image.acquire_release(None, None, self.pool, pathlib.Path("tool.pyz"))

        request.assert_called_once_with("https://pypi.org/pypi/example/2.0/json", timeout=30)

        self.assertEqual(
            publish.call_args.args[2],
            [
                (runtime_wheel, runtime_sha256, runtime_url),
                (self.wheel.name, self.digest, "https://github.com/numpde/nmr-provider-tools/releases/download/v1.2.3/" + self.wheel.name),
            ],
        )

    def test_pypi_mismatch_ambiguity_or_wrong_host_prevents_publication(self):
        runtime_wheel = "example-2.0-py3-none-any.whl"
        runtime_sha256 = "a" * 64
        runtime_url = "https://files.pythonhosted.org/packages/" + runtime_wheel
        self.release["runtime_wheels"] = {runtime_wheel: runtime_sha256}

        for release_files in (
            [{"filename": runtime_wheel, "digests": {"sha256": "b" * 64}}],
            [{"filename": runtime_wheel, "digests": {"sha256": runtime_sha256}, "url": runtime_url}] * 2,
            [{"filename": runtime_wheel, "digests": {"sha256": runtime_sha256}, "url": "https://other.example/file.whl"}],
        ):
            with (
                self.subTest(release_files=release_files),
                patch.object(delivery_image, "require_download_proxy"),
                patch("tools.adapters.pypi_project_json.urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"urls": release_files}).encode())),
                patch.object(delivery_image, "publish_release_wheels") as publish,
                self.assertRaises(ValueError),
            ):
                delivery_image.acquire_release(None, None, self.pool, pathlib.Path("tool.pyz"))

            publish.assert_not_called()

    def test_publisher_receives_the_exact_toml_lock(self):
        wheel_url = "https://example.invalid/provider_core-1.2.3-py3-none-any.whl"

        expected_lock = (
            f'lock-version = "1.0"\n'
            f'created-by = "nmr-provider-tools delivery"\n'
            f'[[packages]]\n'
            f'name = "provider-core"\n'
            f'version = "1.2.3"\n'
            f'[packages.archive]\n'
            f'url = "{wheel_url}"\n'
            f'[packages.archive.hashes]\n'
            f'sha256 = "{self.digest}"\n'
        ).encode("utf-8")

        def inspect_lock(command, *, check):
            generated_lock = pathlib.Path(command[command.index("--lock") + 1])

            self.assertEqual(first=expected_lock, second=generated_lock.read_bytes())

        with patch.object(delivery_image.subprocess, "run", side_effect=inspect_lock) as publish:
            delivery_image.publish_release_wheels(
                self.pool / "tool.pyz",
                self.pool,
                [(self.wheel.name, self.digest, wheel_url)],
            )

        publish.assert_called_once()

    def test_publisher_failure_is_reported(self):
        with patch.object(delivery_image, "require_download_proxy"), patch.object(
            delivery_image,
            "publish_release_wheels",
            side_effect=ValueError("downloaded bytes differ")
        ):
            with self.assertRaisesRegex(ValueError, "downloaded bytes"):
                delivery_image.acquire_release(None, None, self.pool, pathlib.Path("tool.pyz"))

        self.assertFalse(self.wheel.exists())
        self.assertEqual([], list(self.pool.iterdir()))

    def test_capture_records_pins_without_copying_wheels(self):
        selected = {
            "lock_sha256": hashlib.sha256(b"lock").hexdigest(),
            "files": {self.wheel.name: self.digest}
        }

        with tempfile.TemporaryDirectory() as temporary:
            destination = pathlib.Path(temporary)
            dockerfile = destination / "operations/delivery/image/Dockerfile"

            dockerfile.parent.mkdir(parents=True)

            dockerfile.write_text(
                "FROM scratch AS wheel_inputs\n"
                "# selected-wheel-inputs\n"
            )

            record = ROOT / "requirements/installer-release.json"

            (destination / "requirements").mkdir()
            (destination / "requirements/installer-release.json").write_bytes(record.read_bytes())
            installer = json.loads(record.read_text())

            self.wheel.write_bytes(self.content)
            inode = self.wheel.stat().st_ino

            with patch.object(delivery_image, "requirements"), patch.object(
                delivery_image,
                "missing_wheels",
                return_value=[]
            ), patch.object(delivery_image, "select_wheels", return_value=selected):
                delivery_image.capture_image_inputs(None, None, self.pool, destination)

            self.assertEqual(
                {"provider-core-install.lock", "provider-core-wheel-inputs.json", "provider-core-stream.json", "operations", "requirements"},
                {path.name for path in destination.iterdir()},
            )

            self.assertEqual(b"lock", (destination / "provider-core-install.lock").read_bytes())
            self.assertEqual(selected, json.loads((destination / "provider-core-wheel-inputs.json").read_text()))

            self.assertEqual(
                [
                    {"directory": "wheelhouse", "filename": self.wheel.name, "sha256": self.digest},
                    {"directory": "installer", "filename": installer["name"], "sha256": installer["sha256"]}
                ],
                json.loads((destination / "provider-core-stream.json").read_text())
            )

            self.assertIn(self.wheel.name, (dockerfile.parent / "Dockerfile.selected").read_text())
            self.assertEqual(inode, self.wheel.stat().st_ino)

    def test_install_uses_pool_directly_and_checks_captured_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "runtime"

            root.mkdir()
            lock = pathlib.Path(temporary) / "provider-core-install.lock"

            lock.write_bytes(b"lock")

            selected = {
                "lock_sha256": hashlib.sha256(b"lock").hexdigest(),
                "files": {self.wheel.name: self.digest}
            }

            with patch.object(core_environment, "select_wheels", return_value=selected), patch.object(
                core_environment,
                "install_locked",
                return_value=selected
            ) as install, patch.object(core_environment.subprocess, "run"):
                core_environment.install_environment(root, lock, self.pool, selected)

            self.assertEqual(self.pool, install.call_args.kwargs["wheels"])
            self.assertEqual(b"lock", (root / "provider-core-install.lock").read_bytes())
            self.assertFalse((root / "wheels").exists())
