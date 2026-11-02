"""The shared pool preserves locked artifacts through acquisition failures."""

import hashlib
import tempfile
import threading
import tomllib
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from wheelhouse_tool import __main__ as tool


class WheelhouseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.content = b"locked artifact bytes"
        self.name = "example-1.0-py3-none-any.whl"
        (self.root / self.name).write_bytes(self.content)
        self.requests = []
        requests = self.requests

        class Server(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                requests.append(self.path)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Server, directory=str(self.root)))
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.lock = self.root / "pylock.toml"
        self.write_lock(hashlib.sha256(self.content).hexdigest())

    def write_lock(self, digest):
        self.lock.write_text(
            'lock-version = "1.0"\ncreated-by = "test"\n'
            '[[packages]]\nname = "example"\nversion = "1.0"\n'
            '[[packages.wheels]]\n'
            f'url = "http://127.0.0.1:{self.server.server_port}/{self.name}"\n'
            f'hashes = {{ sha256 = "{digest}" }}\n'
        )

    def test_missing_is_downloaded_once_and_cached_without_replacement(self):
        tool.run("acquire", self.lock, self.pool)
        path = self.pool / self.name
        inode = path.stat().st_ino
        tool.run("acquire", self.lock, self.pool)
        self.assertEqual(self.content, path.read_bytes())
        self.assertEqual(inode, path.stat().st_ino)
        self.assertEqual([f"/{self.name}"], self.requests)

    def test_conflict_is_preserved_without_network(self):
        path = self.pool / self.name
        path.write_bytes(b"different")
        inode = path.stat().st_ino
        with self.assertRaisesRegex(ValueError, "existing file is preserved"):
            tool.run("acquire", self.lock, self.pool)
        self.assertEqual((b"different", inode), (path.read_bytes(), path.stat().st_ino))
        self.assertEqual([], self.requests)

    def test_invalid_download_leaves_no_final_or_temporary_file(self):
        self.write_lock("0" * 64)
        with self.assertRaisesRegex(ValueError, "downloaded bytes differ"):
            tool.run("acquire", self.lock, self.pool)
        self.assertEqual([], list(self.pool.iterdir()))

    def test_check_missing_does_not_download(self):
        with self.assertRaisesRegex(FileNotFoundError, "missing"):
            tool.run("check", self.lock, self.pool)
        self.assertEqual([], self.requests)

    def test_simultaneous_acquisition_reuses_the_first_result(self):
        errors = []

        def acquire():
            try:
                tool.run("acquire", self.lock, self.pool)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=acquire) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual([f"/{self.name}"], self.requests)

    def test_required_source_does_not_fall_back_to_wheel(self):
        with self.assertRaisesRegex(ValueError, "source distribution"):
            tool.run("check", self.lock, self.pool, require_sdist=["example"])

    def test_local_lock_preserves_the_selected_hash_and_uses_a_local_path(self):
        projected = tomllib.loads(tool.local_lock(self.lock, Path("/wheels")))
        artifact = projected["packages"][0]["wheels"][0]
        self.assertEqual("/wheels/" + self.name, artifact["path"])
        self.assertNotIn("url", artifact)
        self.assertEqual(hashlib.sha256(self.content).hexdigest(), artifact["hashes"]["sha256"])

    def test_source_must_be_explicitly_allowed(self):
        self.lock.write_text(
            'lock-version = "1.0"\ncreated-by = "test"\n'
            '[[packages]]\nname = "example"\nversion = "1.0"\n'
            '[packages.sdist]\nurl = "https://example.invalid/example-1.0.tar.gz"\n'
            f'hashes = {{ sha256 = "{"0" * 64}" }}\n'
        )
        with self.assertRaisesRegex(ValueError, "source distributions require"):
            tool.run("acquire", self.lock, self.pool)

    def test_direct_archives_obey_source_policy(self):
        def archive(name):
            self.lock.write_text(
                'lock-version = "1.0"\ncreated-by = "test"\n'
                '[[packages]]\nname = "example"\nversion = "1.0"\n'
                '[packages.archive]\n'
                f'url = "https://example.invalid/{name}"\n'
                f'hashes = {{ sha256 = "{"0" * 64}" }}\n'
            )

        archive("example-1.0.tar.gz")
        with self.assertRaisesRegex(ValueError, "source distributions require"):
            tool.selected(self.lock, [])
        self.assertEqual("example-1.0.tar.gz", tool.selected(self.lock, [], ["example"])[0][0])
        archive("example-1.0-py3-none-any.whl")
        self.assertEqual("example-1.0-py3-none-any.whl", tool.selected(self.lock, [])[0][0])
        with self.assertRaisesRegex(ValueError, "source distribution"):
            tool.selected(self.lock, [], ["example"])
        archive("example-1.0-cp27-cp27m-win32.whl")
        with self.assertRaisesRegex(ValueError, "incompatible"):
            tool.selected(self.lock, [])
        archive("other-1.0.tar.gz")
        with self.assertRaisesRegex(ValueError, "does not match"):
            tool.selected(self.lock, [], ["example"])

    def test_credential_in_url_is_rejected_without_disclosure(self):
        lock = self.lock.read_text().replace("http://", "http://user:secret@")
        self.lock.write_text(lock)
        with self.assertRaises(ValueError) as error:
            tool.run("acquire", self.lock, self.pool)
        self.assertNotIn("secret", str(error.exception))

    def test_vanished_concurrent_winner_is_not_reported_as_success(self):
        with patch.object(tool.os, "link", side_effect=FileExistsError):
            with self.assertRaisesRegex(FileNotFoundError, "disappeared"):
                tool.run("acquire", self.lock, self.pool)
        self.assertEqual([], list(self.pool.iterdir()))
