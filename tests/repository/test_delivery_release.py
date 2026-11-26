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
    def test_failed_offline_check_is_fatal_without_missing_classification(self):
        output = io.StringIO()
        arguments = ["build_inputs", "check", "--lock", "/role.json", "--pool", "/pool", "--tool-pin", "/pin.json"]

        with patch("sys.argv", arguments), patch.object(build_inputs, "read_json", return_value={"name": "tool.pyz"}), patch.object(build_inputs, "tool_present", return_value=True), patch.object(build_inputs, "wheelhouse", side_effect=subprocess.CalledProcessError(1, "check")), redirect_stdout(output):
            with self.assertRaises(subprocess.CalledProcessError):
                build_inputs.main()

        self.assertEqual("", output.getvalue())

    def test_lock_uses_new_lock_option_and_explicit_offline_mode(self):
        with patch.object(build_inputs.subprocess, "run") as subprocess_run:
            build_inputs.lock_release(pathlib.Path("tool.pyz"), pathlib.Path("project.toml"), pathlib.Path("pool"), pathlib.Path("role.json"), offline=True)

        subprocess_run.assert_called_once()
        command = subprocess_run.call_args.args[0]

        self.assertIn("--offline", command)
        self.assertEqual("role.json", command[command.index("--lock") + 1])
        self.assertNotIn("--output", command)

    def test_two_origins_share_one_projection_and_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            pool = root / "pool"

            pool.mkdir()
            candidate = root / "private" / "provider_core-1.2.3-py3-none-any.whl"

            candidate.parent.mkdir()
            candidate.write_bytes(b"private candidate")
            dependency = pool / "dependency-1.0-py3-none-any.whl"

            dependency.write_bytes(b"dependency")
            lock = root / "role.json"

            lock.write_text("captured selection")
            destination = root / "context"
            template = destination / "operations/delivery/image/Dockerfile"

            template.parent.mkdir(parents=True)
            template.write_text("FROM scratch\n" "# selected-wheel-inputs\n")
            (destination / "requirements").mkdir()
            installer = {"name": "pip-1-py3-none-any.whl", "sha256": hashlib.sha256(b"pip").hexdigest()}

            (pool / installer["name"]).write_bytes(b"pip")
            artifact_entries = [{"source_path": str(artifact_path), "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(), "filename": artifact_path.name} for artifact_path in (candidate, dependency)]
            selection = {"artifact_entries": artifact_entries}

            def checked(tool, action, selected_lock, selected_pool, *options):
                self.assertEqual("check", action)
                self.assertEqual("captured selection", selected_lock.read_text())

                if options:
                    output = pathlib.Path(options[1])
                    mappings = json.loads(pathlib.Path(options[3]).read_text())["artifacts"]

                    self.assertEqual([(entry["source_path"], entry["sha256"]) for entry in artifact_entries], [(mapping["source_path"], mapping["sha256"]) for mapping in mappings])
                    output.write_text("rendered by Wheelhouse")

                return selection

            with patch.object(build_inputs, "wheelhouse", side_effect=checked), patch.object(build_inputs, "installer_release", return_value=installer):
                build_inputs.capture_image_inputs(lock, pool, destination, pathlib.Path("tool.pyz"))

            manifest = json.loads((destination / "provider-core-stream.json").read_text())
            output = io.BytesIO()

            stream_context(manifest, output)

            with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
                self.assertEqual(b"private candidate", archive.extractfile("wheelhouse/" + candidate.name).read())
                self.assertEqual(b"dependency", archive.extractfile("wheelhouse/" + dependency.name).read())

            self.assertEqual("rendered by Wheelhouse", (destination / "provider-core-install.lock").read_text())
            self.assertEqual([dependency.name, installer["name"]], sorted(artifact_path.name for artifact_path in pool.iterdir()))
            candidate.write_bytes(b"mutated")

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                stream_context(manifest, io.BytesIO())

    def test_projection_failure_never_publishes_stream_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            lock = root / "lock.json"

            lock.write_text("selection")
            destination = root / "context"

            destination.mkdir()

            with patch.object(build_inputs, "wheelhouse", side_effect=[{"artifact_entries": []}, subprocess.CalledProcessError(1, "check")]):
                with self.assertRaises(subprocess.CalledProcessError):
                    build_inputs.capture_image_inputs(lock, root, destination, pathlib.Path("tool.pyz"))

            self.assertFalse((destination / "provider-core-stream.json").exists())

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
