"""Readiness identifies a fresh observation of the current process, not a Job outcome."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.process_status import ProcessStatus, check_status


class ProcessStatusTests(unittest.TestCase):
    def test_ready_working_and_retirement_have_distinct_meanings(self):
        with tempfile.TemporaryDirectory() as root:
            status = ProcessStatus(Path(root), "reader")
            status.refresh("starting")
            with self.assertRaisesRegex(ValueError, "starting"):
                check_status(Path(root) / "status.json", "reader")
            for phase in ("ready", "working"):
                status.refresh(phase)
                check_status(Path(root) / "status.json", "reader")
            status.refresh("stopping")
            with self.assertRaisesRegex(ValueError, "stopping"):
                check_status(Path(root) / "status.json", "reader")
            self.assertEqual((Path(root) / "status.json").stat().st_mode & 0o777, 0o600)

    def test_stale_observation_and_reused_pid_are_not_ready(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "status.json"
            status = ProcessStatus(Path(root), "provider")
            status.refresh("ready")
            value = json.loads(path.read_bytes())
            with patch("provider_core.process_status.time.monotonic", return_value=value["updated_monotonic"] + 31):
                with self.assertRaisesRegex(ValueError, "expired"):
                    check_status(path, "provider")
            value["start_ticks"] += 1
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "process identity"):
                check_status(path, "provider")

    def test_status_writer_does_not_follow_a_stale_temporary_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            victim = root / "victim"
            victim.write_text("unchanged")
            (root / ".status.pending").symlink_to(victim)
            with self.assertRaises(OSError):
                ProcessStatus(root, "proxy").refresh("ready")
            self.assertEqual(victim.read_text(), "unchanged")

    def test_failed_replacement_leaves_previous_observation_only_until_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            status = ProcessStatus(root, "reader")
            status.refresh("ready")
            before = (root / "status.json").read_bytes()
            with patch("provider_core.process_status.os.replace", side_effect=OSError("authored replacement failure")):
                with self.assertRaises(OSError):
                    status.refresh("stopping")
            self.assertEqual((root / "status.json").read_bytes(), before)
            check_status(root / "status.json", "reader")
            with patch("provider_core.process_status.time.monotonic", return_value=json.loads(before)["updated_monotonic"] + 31):
                with self.assertRaisesRegex(ValueError, "expired"):
                    check_status(root / "status.json", "reader")
