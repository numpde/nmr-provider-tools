"""Exercise shared-cache publication and captured-byte authority offline."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import checker_dependencies as dependencies


class CheckerDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.capture = self.root / "capture"
        self.capture.mkdir()
        self.package = {
            "name": "make", "version": "4.3-4.1", "file": "make_4.3-4.1_amd64.deb",
            "url": "https://deb.debian.org/debian/pool/main/m/make-dfsg/make_4.3-4.1_amd64.deb",
            "size": 4, "sha256": hashlib.sha256(b"make").hexdigest(),
        }
        self.artifact = self.pool / self.package["file"]
        self.artifact.write_bytes(b"make")
        self.manifest = self.root / "dependencies.json"
        self.manifest.write_text(json.dumps({"platform": "linux/amd64", "packages": [self.package]}))

    def test_declaration_rejects_duplicate_fields_and_foreign_artifacts(self):
        self.assertEqual([self.package], dependencies.declaration(self.manifest))
        self.manifest.write_text('{"platform":"linux/amd64","platform":"linux/amd64","packages":[]}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
            dependencies.declaration(self.manifest)
        for changes in ({"name": "ruff"}, {"file": "../make.deb"}, {"size": True}, {"url": "https://other.invalid/make.deb"}):
            with self.subTest(changes=changes):
                package = self.package | changes
                self.manifest.write_text(json.dumps({"platform": "linux/amd64", "packages": [package]}))
                with self.assertRaises(ValueError):
                    dependencies.declaration(self.manifest)

    def test_shared_pool_ignores_unrelated_files_and_captures_only_declared_bytes(self):
        unrelated = self.pool / "another-project.whl"
        unrelated.write_bytes(b"unrelated")
        dependencies.capture([self.package], self.pool, self.capture)
        self.assertEqual([self.package["file"]], [path.name for path in self.capture.iterdir()])
        self.assertEqual(b"make", (self.capture / self.package["file"]).read_bytes())
        self.assertEqual(b"unrelated", unrelated.read_bytes())
        self.artifact.write_bytes(b"late")
        self.assertEqual(b"make", (self.capture / self.package["file"]).read_bytes())

    def test_capture_rejects_bytes_changed_after_selection(self):
        original = dependencies.check

        def change_after_check(packages, pool):
            original(packages, pool)
            self.artifact.write_bytes(b"late")

        with patch.object(dependencies, "check", side_effect=change_after_check):
            with self.assertRaisesRegex(ValueError, "differs"):
                dependencies.capture([self.package], self.pool, self.capture)

    def test_artifact_symlinks_and_oversized_bytes_are_rejected(self):
        linked = self.pool / "linked"
        linked.symlink_to(self.artifact)
        with self.assertRaises(OSError):
            dependencies.verify(self.package, linked)
        self.artifact.write_bytes(b"make-extra")
        with self.assertRaisesRegex(ValueError, "exceeds declared size"):
            dependencies.verify(self.package, self.artifact)

    def test_publication_keeps_identical_winner_inode_and_preserves_conflict(self):
        staged = self.capture / self.package["file"]
        staged.write_bytes(b"make")
        inode = self.artifact.stat().st_ino
        dependencies.publish(self.package, staged, self.artifact)
        self.assertEqual(inode, self.artifact.stat().st_ino)
        self.artifact.write_bytes(b"late")
        with self.assertRaisesRegex(ValueError, "differs"):
            dependencies.publish(self.package, staged, self.artifact)
        self.assertEqual(b"late", self.artifact.read_bytes())
        self.assertEqual(b"make", staged.read_bytes())

    def test_publication_does_not_replace_symlinks_and_links_new_files(self):
        staged = self.capture / self.package["file"]
        staged.write_bytes(b"make")
        destination = self.pool / "new"
        dependencies.publish(self.package, staged, destination)
        self.assertEqual(staged.stat().st_ino, destination.stat().st_ino)
        destination.unlink()
        destination.symlink_to(self.artifact)
        with self.assertRaises(OSError):
            dependencies.publish(self.package, staged, destination)
        self.assertTrue(destination.is_symlink())

    def test_download_refuses_unowned_network_configuration_before_writes(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "launcher-owned loopback proxy"):
                dependencies.download([self.package], self.pool)
        self.assertEqual([self.artifact], list(self.pool.iterdir()))
