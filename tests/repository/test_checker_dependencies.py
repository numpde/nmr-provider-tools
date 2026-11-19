"""Exercise shared-cache publication and captured-byte authority offline."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
import pathlib
from http.client import HTTPMessage
from unittest.mock import call, patch

from operations.checker.image import dependencies


class CheckerDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.pool = self.root / "pool"

        self.pool.mkdir()
        self.capture = self.root / "capture"

        self.capture.mkdir()

        self.package = {
            "name": "make",
            "version": "4.3-4.1",
            "file": "make_4.3-4.1_amd64.deb",
            "url": "https://deb.debian.org/debian/pool/main/m/make-dfsg/make_4.3-4.1_amd64.deb",
            "size": 4,
            "sha256": hashlib.sha256(b"make").hexdigest(),
        }

        self.artifact = self.pool / self.package["file"]

        self.artifact.write_bytes(b"make")
        self.manifest = self.root / "dependencies.json"

        self.manifest.write_text(json.dumps({"platform": "linux/amd64", "packages": [self.package]}))

    def test_declaration_rejects_duplicate_fields_and_foreign_artifacts(self):
        self.assertEqual([self.package], dependencies.declaration(self.manifest))
        self.manifest.write_text('{"platform":"linux/amd64","platform":"linux/amd64","packages":[]}')

        with self.assertRaises(ValueError) as caught:
            dependencies.declaration(self.manifest)

        self.assertIn("'platform': 1", str(caught.exception))

        for (changes, error_type) in (
            ({"name": "ruff"}, ValueError),
            ({"file": "../make.deb"}, ValueError),
            ({"size": True}, TypeError),
            ({"url": "https://other.invalid/make.deb"}, ValueError),
        ):
            with self.subTest(changes=changes):
                package = self.package | changes

                self.manifest.write_text(json.dumps({"platform": "linux/amd64", "packages": [package]}))

                with self.assertRaises(error_type):
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
            with self.assertRaises(ValueError):
                dependencies.capture([self.package], self.pool, self.capture)

    def test_artifact_symlinks_and_oversized_bytes_are_rejected(self):
        linked = self.pool / "linked"

        linked.symlink_to(self.artifact)

        with self.assertRaises(OSError):
            dependencies.verify(self.package, linked)

        self.artifact.write_bytes(b"make-extra")

        with self.assertRaises(ValueError):
            dependencies.verify(self.package, self.artifact)

    def test_publication_keeps_identical_winner_inode_and_preserves_conflict(self):
        staged = self.capture / self.package["file"]

        staged.write_bytes(b"make")
        inode = self.artifact.stat().st_ino

        dependencies.publish(self.package, staged, self.artifact)
        self.assertEqual(inode, self.artifact.stat().st_ino)
        self.artifact.write_bytes(b"late")

        with self.assertRaises(ValueError):
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

    def test_download_refuses_http_redirect_before_sending_or_publishing(self):
        self.artifact.unlink()
        headers = HTTPMessage()
        headers["Location"] = "http://mirror.invalid/make.deb"
        proxy = "http://127.0.0.1:12345"
        proxy_environment = {name: proxy for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}

        with (
            urllib.error.HTTPError(self.package["url"], 302, "Found", headers, None) as redirect,
            patch.dict(os.environ, proxy_environment, clear=True),
            patch.object(urllib.request.HTTPSHandler, "https_open", return_value=redirect) as open_https,
            patch.object(urllib.request.HTTPHandler, "http_open") as open_http,
        ):
            with self.assertRaises(ValueError):
                dependencies.download([self.package], self.pool)

            open_https.assert_called_once()
            open_http.assert_not_called()

        self.assertEqual([], list(self.pool.iterdir()))

    def test_install_verifies_artifact_before_invoking_dpkg(self):
        self.artifact.write_bytes(b"late")

        with patch.object(subprocess, "run") as package_install_dispatch:
            with self.assertRaises(ValueError):
                dependencies.install([self.package], self.pool)

            package_install_dispatch.assert_not_called()

    def test_install_compares_the_exact_version_reported_after_installation(self):
        installed_version = self.package["version"] + "\n"
        install_command = ["dpkg", "--install", str(self.artifact)]
        version_command = ["dpkg-query", "--show", "--showformat=${Version}", "make"]

        with patch.object(
            subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess(install_command, 0),
                subprocess.CompletedProcess(version_command, 0, stdout=installed_version),
            ],
        ) as dpkg_invocations:
            with self.assertRaises(ValueError):
                dependencies.install([self.package], self.pool)

        self.assertEqual(
            [call(install_command, check=True), call(version_command, check=True, capture_output=True, text=True)],
            dpkg_invocations.call_args_list,
        )
