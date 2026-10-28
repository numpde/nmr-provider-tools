"""Scientific workers use installed native helpers without API dependencies."""

import hashlib
import importlib.metadata
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from provider_core.upload_file import VerifiedUploadFile, UploadFileReadFailed, read_upload_file
from tests.fakes.runner import RunningSupervisor, worker_pid


class NativeInstallationTests(unittest.TestCase):
    def test_native_imports_have_no_third_party_dependency_closure(self):
        for name in ("httpx", "httpcore", "http_sf", "cryptography", "tomli_w"):
            self.assertIsNone(importlib.util.find_spec(name), name)
        import provider_core.runner
        import provider_core.runner_protocol
        import provider_core.runner_session
        import provider_core.process_status
        import provider_core.lifetime
        distribution = importlib.metadata.distribution("provider-core")
        self.assertTrue(distribution.requires)
        self.assertTrue(all('; extra == "api"' in requirement for requirement in distribution.requires))
        self.assertEqual({"provider-core"}, {item.metadata["Name"] for item in importlib.metadata.distributions()})

    def test_native_runner_retires_its_real_child_and_releases_scratch(self):
        runner = RunningSupervisor(operation_seconds=None)
        self.addCleanup(runner.close)
        session = runner.session(startup_seconds=2, retirement_seconds=3)
        self.addCleanup(session.close)
        session.connect()
        self.assertGreater(worker_pid(session.science), 0)
        session.observe_retirement()
        self.assertEqual(0, session.retirement.returncode)
        self.assertTrue(session.retirement.scratch_released)

    def test_native_reader_returns_verified_bytes_and_refuses_later_mutation(self):
        data = b"scientific input"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            path.write_bytes(data)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                receipt = VerifiedUploadFile("job", "upload", len(data), "sha256:" + hashlib.sha256(data).hexdigest(), "source")
                snapshot = read_upload_file(receipt, directory_fd=descriptor, maximum_bytes=64)
                self.assertEqual(data, snapshot.data)
                path.write_bytes(b"X" * len(data))
                with self.assertRaises(UploadFileReadFailed):
                    read_upload_file(receipt, directory_fd=descriptor, maximum_bytes=64)
                self.assertEqual(data, snapshot.data)
            finally:
                os.close(descriptor)
