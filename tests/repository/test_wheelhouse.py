"""Real pip reuses a partial flat pool and preserves its existing files."""

from tools.container_guard import require_container

require_container()

import hashlib
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from tools import wheelhouse


class WheelhouseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "shared pool"
        self.pool.mkdir()
        self.index = self.root / "index"
        self.index.mkdir()
        self.cached = self.make_wheel("pool-cached")
        self.missing = self.make_wheel("pool-missing")
        shutil.copyfile(self.cached, self.pool / self.cached.name)
        self.lock = self.root / "requirements.lock"
        self.lock.write_text("".join(
            f"{name}==1.0 " + "\\\n" + f"    --hash=sha256:{wheelhouse.digest(path)}\n"
            for name, path in (("pool-cached", self.cached), ("pool-missing", self.missing))
        ))

    def make_wheel(self, name):
        distribution = name.replace("-", "_")
        path = self.index / f"{distribution}-1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            prefix = f"{distribution}-1.0.dist-info"
            archive.writestr(f"{prefix}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
            archive.writestr(f"{prefix}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(f"{prefix}/RECORD", "")
        package_index = self.index / "simple" / name
        package_index.mkdir(parents=True)
        package_index.joinpath("index.html").write_text(
            f'<a href="../../{path.name}#sha256={wheelhouse.digest(path)}">{path.name}</a>'
        )
        return path

    def local_index(self):
        requests = []

        class Index(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                requests.append(self.path)

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Index, directory=str(self.index)))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        run = subprocess.run

        def use_local_index(command, **kwargs):
            command = list(command)
            if "download" in command:
                command[command.index("--index-url") + 1] = f"http://127.0.0.1:{server.server_port}/simple"
            return run(command, **kwargs)

        return requests, use_local_index

    def test_complete_pool_needs_no_download_grant_and_keeps_existing_inodes(self):
        shutil.copyfile(self.missing, self.pool / self.missing.name)
        before = {path.name: (path.read_bytes(), path.stat().st_ino) for path in self.pool.iterdir()}
        with patch.object(wheelhouse, "require_download_proxy", side_effect=AssertionError("No acquisition needed")):
            wheelhouse.populate_pool(self.lock, self.pool)
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_ino) for path in self.pool.iterdir()})

    def test_partial_pool_downloads_only_missing_wheel_then_captures_offline(self):
        unrelated = self.pool / "other-project.txt"
        unrelated.write_bytes(b"preserve unrelated bytes")
        cached = self.pool / self.cached.name
        inode = cached.stat().st_ino
        requests, run = self.local_index()
        with patch.object(wheelhouse, "require_download_proxy"), patch.object(wheelhouse.subprocess, "run", side_effect=run):
            wheelhouse.populate_pool(self.lock, self.pool)
        self.assertEqual([f"/{self.missing.name}"], [path for path in requests if path.endswith(".whl")])
        self.assertEqual(inode, cached.stat().st_ino)
        self.assertEqual(self.cached.read_bytes(), cached.read_bytes())
        self.assertEqual(b"preserve unrelated bytes", unrelated.read_bytes())
        destination = self.root / "capture"
        destination.mkdir()
        identity = wheelhouse.capture(self.lock, self.pool, destination)
        self.assertEqual({self.cached.name, self.missing.name}, set(identity["files"]))
        self.assertEqual(identity["files"], {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in destination.iterdir()})
        self.assertEqual([], list(self.pool.glob(".provider-core-download-*")))

    def test_conflicting_cached_bytes_are_preserved_and_private_staging_is_cleaned(self):
        cached = self.pool / self.cached.name
        cached.write_bytes(b"conflicting cached bytes")
        inode = cached.stat().st_ino
        _requests, run = self.local_index()
        with patch.object(wheelhouse, "require_download_proxy"), patch.object(wheelhouse.subprocess, "run", side_effect=run):
            with self.assertRaises(FileExistsError):
                wheelhouse.populate_pool(self.lock, self.pool)
        self.assertEqual(b"conflicting cached bytes", cached.read_bytes())
        self.assertEqual(inode, cached.stat().st_ino)
        self.assertEqual([], list(self.pool.glob(".provider-core-download-*")))
