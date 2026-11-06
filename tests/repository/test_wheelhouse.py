"""Pip selects locked wheels from the shared pool."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from tools import provider_core_acquire, wheelhouse


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

    def make_wheel(self, name, version="1.0"):
        distribution = name.replace("-", "_")
        path = self.index / f"{distribution}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            prefix = f"{distribution}-{version}.dist-info"
            archive.writestr(f"{prefix}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
            archive.writestr(f"{prefix}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(f"{prefix}/RECORD", "")
        return path

    def test_provider_core_cache_check_distinguishes_missing_and_corrupt(self):
        self.assertEqual("missing", provider_core_acquire.cache_state(self.pool, [self.lock]))
        shutil.copyfile(self.missing, self.pool / self.missing.name)
        self.assertEqual("complete", provider_core_acquire.cache_state(self.pool, [self.lock]))
        (self.pool / self.missing.name).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "cached bytes differ"):
            provider_core_acquire.cache_state(self.pool, [self.lock])

    def test_provider_core_cache_check_reuses_admitted_local_version(self):
        wheel = self.make_wheel("pool-local", "1.0+cpu")
        shutil.copyfile(wheel, self.pool / wheel.name)
        self.lock.write_text(f"pool-local==1.0 \\\n    --hash=sha256:{wheelhouse.digest(wheel)}\n")
        self.assertEqual("complete", provider_core_acquire.cache_state(self.pool, [self.lock]))

        self.lock.write_text(self.lock.read_text().replace("==1.0 ", "==1.0+other "))
        self.assertEqual("missing", provider_core_acquire.cache_state(self.pool, [self.lock]))
        self.lock.write_text(self.lock.read_text().replace("==1.0+other ", "==1.0 "))
        (self.pool / wheel.name).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "cached bytes differ"):
            provider_core_acquire.cache_state(self.pool, [self.lock])
        alternate = self.make_wheel("pool-local", "1.0+z")
        shutil.copyfile(alternate, self.pool / alternate.name)
        self.lock.write_text(f"pool-local==1.0 \\\n    --hash=sha256:{wheelhouse.digest(wheel)} \\\n"
                             f"    --hash=sha256:{wheelhouse.digest(alternate)}\n")
        self.assertEqual("complete", provider_core_acquire.cache_state(self.pool, [self.lock]))

    def test_acquisition_bootstrap_dependency_is_checked_outside_runtime_lock(self):
        lock = self.root / "bootstrap.toml"
        lock.write_text('[[packages]]\nname = "identity-fixture"\nversion = "1.0"\n'
                        '[[packages.wheels]]\nurl = "https://example.invalid/' + self.missing.name + '"\n'
                        'hashes = {sha256 = "' + wheelhouse.digest(self.missing) + '"}\n')
        self.assertFalse(provider_core_acquire.bootstrap_present(self.pool, lock))
        shutil.copyfile(self.missing, self.pool / self.missing.name)
        self.assertTrue(provider_core_acquire.bootstrap_present(self.pool, lock))
        (self.pool / self.missing.name).write_bytes(b"corrupt bootstrap")
        with self.assertRaisesRegex(ValueError, "bootstrap pin"):
            provider_core_acquire.bootstrap_present(self.pool, lock)

    def test_pinned_tool_bootstrap_downloads_once_and_reuses(self):
        content = b"synthetic tool release"
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            pin = {"name": "synthetic-tool.pyz", "sha256": hashlib.sha256(content).hexdigest(),
                   "url": f"http://127.0.0.1:{server.server_port}/synthetic-tool.pyz"}
            with patch.object(provider_core_acquire, "require_download_proxy"):
                path = provider_core_acquire.acquire_tool(self.pool, pin)
                provider_core_acquire.acquire_tool(self.pool, pin)
            self.assertEqual(content, path.read_bytes())
            self.assertEqual(["/synthetic-tool.pyz"], requests)
            self.assertFalse(list(self.pool.glob("*.part")))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_cached_tool_is_reusable_during_shared_pool_access(self):
        content = b"synthetic tool release"
        pin = {"name": "synthetic-tool.pyz", "sha256": hashlib.sha256(content).hexdigest()}
        (self.pool / pin["name"]).write_bytes(content)
        finished = Event()
        errors = []

        def acquire():
            try:
                provider_core_acquire.acquire_tool(self.pool, pin)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        with wheelhouse.pool_lock(self.pool, exclusive=False):
            thread = Thread(target=acquire)
            thread.start()
            completed_while_shared = finished.wait(2)
        thread.join(5)
        self.assertTrue(completed_while_shared, msg="cached tool waited for exclusive pool access")
        self.assertEqual([], errors)

    def test_report_requires_the_actual_candidate_wheel(self):
        candidate_dir = self.root / "release"
        candidate_dir.mkdir()
        candidate = candidate_dir / self.cached.name
        shutil.copyfile(self.cached, candidate)
        report = self.root / "report.json"

        def write_report(path):
            report.write_text(json.dumps({"version": "1", "install": [{"download_info": {
                "url": path.as_uri(), "archive_info": {"hashes": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}}]}))

        write_report(self.pool / self.cached.name)
        with self.assertRaisesRegex(ValueError, "required release artifact"):
            wheelhouse.wheel_inputs_from_report(self.lock, self.pool, report, (candidate_dir,), {candidate.name: candidate})
        write_report(candidate)
        selected = wheelhouse.wheel_inputs_from_report(self.lock, self.pool, report, (candidate_dir,), {candidate.name: candidate})
        self.assertIn(candidate.name, selected["files"])
