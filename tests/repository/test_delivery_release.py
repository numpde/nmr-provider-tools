"""Delivery captures exact artifact identities and requires both build processes."""

from contextlib import redirect_stdout
import hashlib
import io
import json
import re
import pathlib
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from operations.delivery import build_inputs
from acquisition_tool import context as exporter
from acquisition_tool.context import stream_context


class DeliveryReleaseTests(unittest.TestCase):
    def test_failed_offline_check_is_fatal(self):
        output = io.StringIO()
        arguments = ["build_inputs", "check", "--pin", "/pin.json", "--runtime-lock", "/runtime.lock", "--pool", "/pool"]

        with patch("sys.argv", arguments), patch.object(build_inputs, "check_release", side_effect=subprocess.CalledProcessError(1, "pip")), redirect_stdout(output):
            with self.assertRaises(subprocess.CalledProcessError):
                build_inputs.main()

        self.assertEqual("", output.getvalue())

    def test_captured_selection_streams_verified_bytes_and_hashed_installation_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            pool = root / "pool"
            pool.mkdir()
            dependency = pool / "dependency-1.0-py3-none-any.whl"
            dependency.write_bytes(b"dependency")
            pin = root / "release.json"

            pin.write_text("captured release")
            lock = root / "runtime.lock"

            lock.write_text("captured runtime")
            destination = root / "context"
            template = destination / "operations/delivery/image/Dockerfile"
            template.parent.mkdir(parents=True)
            template.write_text("FROM scratch\n" "# selected-wheel-inputs\n")
            (destination / "requirements").mkdir()
            installer = {"name": "pip-1-py3-none-any.whl", "sha256": hashlib.sha256(b"pip").hexdigest()}
            (pool / installer["name"]).write_bytes(b"pip")
            checksum = hashlib.sha256(dependency.read_bytes()).hexdigest()

            def checked(captured_pin, captured_lock, selected_pool):
                self.assertEqual("captured release", captured_pin.read_text())
                self.assertEqual("captured runtime", captured_lock.read_text())
                self.assertEqual(pool, selected_pool)

                return {"files": {dependency.name: checksum}}

            with patch.object(build_inputs, "check_release", side_effect=checked), patch.object(build_inputs, "installer_release", return_value=installer):
                build_inputs.capture_image_inputs(pin, lock, pool, destination)

            manifest = json.loads((destination / "provider-core-stream.json").read_text())
            output = io.BytesIO()
            stream_context(manifest, output)

            with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
                self.assertEqual(b"dependency", archive.extractfile("wheelhouse/" + dependency.name).read())

            self.assertEqual(
                "file:///wheels/" + dependency.name + " --hash=sha256:" + checksum + "\n",
                (destination / "provider-core-install.lock").read_text(),
            )

            dependency.write_bytes(b"mutated")

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                stream_context(manifest, io.BytesIO())

    def test_failed_selection_never_publishes_stream_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            pin = root / "release.json"

            pin.write_text("release")
            lock = root / "runtime.lock"

            lock.write_text("runtime")
            destination = root / "context"
            destination.mkdir()

            with patch.object(build_inputs, "check_release", side_effect=subprocess.CalledProcessError(1, "pip")):
                with self.assertRaises(subprocess.CalledProcessError):
                    build_inputs.capture_image_inputs(pin, lock, root, destination)

            self.assertFalse((destination / "provider-core-stream.json").exists())

    def test_changed_runtime_lock_rejects_recorded_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            lock = root / "runtime.lock"

            lock.write_text("changed")
            pin = root / "release.json"

            pin.write_text(json.dumps({"version": "1.2.3", "wheel_sha256": "a" * 64, "install_lock_sha256": "b" * 64}))

            with self.assertRaisesRegex(ValueError, "install_lock_sha256"):
                build_inputs.check_release(pin, lock, root)

    def test_build_pipeline_rejects_late_export_or_builder_failure(self):
        script = pathlib.Path(build_inputs.__file__).with_name("build_image.sh").read_text()
        match = re.search(r"900 stdio bash -o pipefail -c '(.*?)' bash", script, re.DOTALL)

        self.assertIsNotNone(match)

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            source = root / "payload"

            source.write_bytes(b"verified")
            manifest = root / "manifest.json"

            manifest.write_text(json.dumps([{"path": str(source), "destination": "payload", "sha256": hashlib.sha256(b"verified").hexdigest(), "executable": False}]))

            for (valid_bytes, builder_status) in ((True, 0), (False, 0), (True, 8)):
                # silent-defaults: allow-contract -- The fixture deliberately chooses matching or corrupted bytes to exercise both exporter outcomes.
                source.write_bytes(b"verified" if valid_bytes else b"mutated!")

                builder_command = f"cat >/dev/null; exit {builder_status}"  # machine-string: The shell exit command requires a numeric status without expression labels.

                pipeline_result = subprocess.run(
                    [
                        "bash",  # Use the same shell as the production pipeline.
                        "-o",  # Enable the production pipeline status policy.
                        "pipefail",  # Require the exporter and builder to succeed.
                        "-c",  # Evaluate the captured production pipeline body.
                        match[1],  # Exercise the actual delivery build command.
                        "bash",  # Name this shell invocation.
                        exporter.__file__,  # Execute the shared context producer.
                        str(manifest),  # Supply the captured file identities.
                        "bash",  # Simulate the receiving builder process.
                        "-c",  # Consume the entire stream before reporting status.
                        builder_command,  # Return the chosen builder outcome.
                    ],
                    stderr=subprocess.PIPE,
                )

                self.assertEqual(valid_bytes and (builder_status == 0), pipeline_result.returncode == 0)


if __name__ == "__main__":
    unittest.main()
