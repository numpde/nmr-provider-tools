"""Durable recovery records are private, exclusive and never silently replaced."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from provider_deploy.journal import Journal
from provider_deploy.model import StepError


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_private_exclusive_durable_append(self):
        path = self.root / "record"
        with patch("provider_deploy.journal.os.fsync", wraps=os.fsync) as sync:
            journal = Journal(path)
            journal.append({"state": "uncertain-outcome", "event": "create-intent"})
            journal.close()
        self.assertGreaterEqual(sync.call_count, 2)  # directory and event data
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual("uncertain-outcome", json.loads(path.read_text())["state"])
        with self.assertRaises(FileExistsError):
            Journal(path)

    def test_shared_parent_is_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaises(StepError) as caught:
            Journal(self.root / "record")
        self.assertEqual("journal", caught.exception.category)
        self.assertFalse((self.root / "record").exists())

    def test_final_component_symlinks_are_not_followed(self):
        target = self.root / "target"
        target.write_text("retain")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            Journal(link)
        self.assertEqual("retain", target.read_text())

    def test_parent_symlink_is_rejected(self):
        directory = self.root / "private"
        directory.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(directory)
        with self.assertRaises(OSError):
            Journal(link / "record")
        self.assertFalse((directory / "record").exists())

    def test_partial_writes_preserve_complete_record(self):
        original = os.write
        with patch("provider_deploy.journal.os.write", side_effect=lambda fd, data: original(fd, data[:3])):
            journal = Journal(self.root / "record")
            journal.append({"state": "uncertain-outcome"})
            journal.close()
        self.assertEqual("uncertain-outcome", json.loads((self.root / "record").read_text())["state"])


if __name__ == "__main__":
    unittest.main()
