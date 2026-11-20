"""Readiness identifies a fresh observation of the current process, not a Job outcome."""

import json
import io
import os
import tempfile
import unittest
import pathlib
from contextlib import redirect_stdout
from unittest.mock import patch

from provider_core.process_status import ProcessStatus, check_status, main


class ProcessStatusTests(unittest.TestCase):
    def test_operator_detail_ages_advance_and_writes_are_throttled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status = ProcessStatus(root, "delivery")
            details = {"queue": {"oldest_pending_age_seconds": 10}, "queue_observation_age_seconds": 2}

            status.refresh("ready", details=details)
            before = (root / "status.json").read_bytes()
            written = json.loads(before)["updated_monotonic"]

            with patch("provider_core.process_status.time.monotonic", return_value=written + 3):
                status.refresh("ready", details={"queue": None, "queue_observation_age_seconds": 0})
                self.assertEqual((root / "status.json").read_bytes(), before)
                output = io.StringIO()

                with patch("sys.argv", ["status", str(root / "status.json"), "delivery", "--details"]), redirect_stdout(output):
                    main()

                status_report = json.loads(output.getvalue())

                self.assertEqual(status_report["observation_age_seconds"], 3)
                self.assertEqual(status_report["queue_observation_age_seconds"], 5)
                self.assertEqual(status_report["queue"]["oldest_pending_age_seconds"], 13)
                status.refresh("ready")
                self.assertNotIn("details", check_status(root / "status.json", "delivery"))

    def test_ready_working_and_retirement_have_distinct_meanings(self):
        with tempfile.TemporaryDirectory() as root:
            status = ProcessStatus(pathlib.Path(root), "reader")

            status.refresh("starting")

            with self.assertRaisesRegex(ValueError, "starting"):
                check_status(pathlib.Path(root) / "status.json", "reader")

            for phase in ("ready", "working"):
                status.refresh(phase)
                check_status(pathlib.Path(root) / "status.json", "reader")

            status.refresh("stopping")

            with self.assertRaisesRegex(ValueError, "stopping"):
                check_status(pathlib.Path(root) / "status.json", "reader")

            self.assertEqual((pathlib.Path(root) / "status.json").stat().st_mode & 0o777, 0o600)

    def test_stale_observation_and_reused_pid_are_not_ready(self):
        with tempfile.TemporaryDirectory() as root:
            status_path = pathlib.Path(root) / "status.json"
            status = ProcessStatus(pathlib.Path(root), "provider")

            status.refresh("ready")
            status_record = json.loads(status_path.read_bytes())

            with patch("provider_core.process_status.time.monotonic", return_value=status_record["updated_monotonic"] + 31):
                with self.assertRaises(ValueError):
                    check_status(status_path, "provider")

            status_record["start_ticks"] += 1

            status_path.write_text(json.dumps(status_record))

            with self.assertRaises(ValueError):
                check_status(status_path, "provider")

    def test_status_writer_does_not_follow_a_stale_temporary_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            victim = root / "victim"

            victim.write_text("unchanged")
            (root / ".status.pending").symlink_to(victim)

            with self.assertRaises(OSError):
                ProcessStatus(root, "proxy").refresh("ready")

            self.assertEqual(victim.read_text(), "unchanged")

    def test_failed_replacement_leaves_previous_observation_only_until_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            status = ProcessStatus(root, "reader")

            status.refresh("ready")
            before = (root / "status.json").read_bytes()

            with patch("provider_core.process_status.os.replace", side_effect=OSError("authored replacement failure")):
                with self.assertRaises(OSError):
                    status.refresh("stopping")

            self.assertEqual((root / "status.json").read_bytes(), before)
            check_status(root / "status.json", "reader")

            with patch("provider_core.process_status.time.monotonic", return_value=json.loads(before)["updated_monotonic"] + 31):
                with self.assertRaises(ValueError):
                    check_status(root / "status.json", "reader")
