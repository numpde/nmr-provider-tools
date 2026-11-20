"""Preserve read failure and close evidence without swallowing interruption."""

import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from provider_core.startup_file import StartupFileRejected, read_startup_file


class StartupFileOwnershipTests(unittest.TestCase):
    def test_read_and_close_failures_keep_the_original_read_outcome(self):
        native_close = os.close

        with tempfile.TemporaryDirectory() as directory:
            startup_path = pathlib.Path(directory) / "configuration"

            startup_path.write_bytes(b"configuration")

            for (read_failure, expected_type) in [
                (OSError("Read failed"), StartupFileRejected),
                (KeyboardInterrupt("Read interrupted"), KeyboardInterrupt),
            ]:
                with self.subTest(expected_type=expected_type):
                    close_failure = OSError("Close failed")

                    def fail_after_close(descriptor):
                        # Release the real test descriptor before simulating a close error
                        native_close(descriptor)

                        raise close_failure

                    with (
                        patch("provider_core.startup_file.os.read", side_effect=read_failure),
                        patch("provider_core.startup_file.os.close", side_effect=fail_after_close),
                        self.assertRaises(expected_type) as rejected,
                    ):
                        read_startup_file(startup_path, maximum_bytes=1024)

                    if isinstance(read_failure, KeyboardInterrupt):
                        self.assertIs(read_failure, rejected.exception)
                        self.assertIsInstance(rejected.exception.__cause__, StartupFileRejected)
                        self.assertEqual((close_failure,), rejected.exception.__cause__.cleanup_failures)
                    else:
                        self.assertIs(read_failure, rejected.exception.cause)
                        self.assertEqual((close_failure,), rejected.exception.cleanup_failures)
