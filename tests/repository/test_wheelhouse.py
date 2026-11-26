"""Pip selects locked wheels from the shared pool."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
import pathlib
import urllib.request
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from unittest.mock import patch

from sdk_build import provider_core_acquire
from tools import wheelhouse


class WheelhouseTests(unittest.TestCase):
    def test_required_artifact_beats_a_higher_build_in_the_shared_pool(self):
        competing_wheel = self.pool / "pool_cached-1.0-1higher-py3-none-any.whl"

        shutil.copyfile(self.cached, competing_wheel)

        with zipfile.ZipFile(competing_wheel, "a") as archive:
            archive.writestr("different-build.txt", "Another source revision")

        selected = wheelhouse.select_wheels(
            self.lock,
            self.pool,
            extra_pools=(self.index,),
            required_files={self.cached.name: self.cached},
        )

        self.assertEqual(wheelhouse.digest(self.cached), selected["files"][self.cached.name])
        self.assertNotIn(competing_wheel.name, selected["files"])

    def test_required_artifact_cannot_override_the_original_lock_hash(self):
        with zipfile.ZipFile(self.cached, "a") as archive:
            archive.writestr("unreviewed.txt", "Bytes outside the original lock")

        with self.assertRaises(subprocess.CalledProcessError):
            wheelhouse.select_wheels(
                self.lock,
                self.pool,
                extra_pools=(self.index,),
                required_files={self.cached.name: self.cached},
            )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.pool = self.root / "shared pool"

        self.pool.mkdir()
        self.index = self.root / "index"

        self.index.mkdir()
        self.cached = self.make_wheel("pool-cached")
        self.missing = self.make_wheel("pool-missing")

        shutil.copyfile(self.cached, self.pool / self.cached.name)
        self.lock = self.root / "requirements.lock"

        self.lock.write_text(
            "".join(
                (f"{name}==1.0 " + "\\\n" + f"    --hash=sha256:{wheelhouse.digest(wheel_path)}\n")
                for (name, wheel_path) in (("pool-cached", self.cached), ("pool-missing", self.missing))
            )
        )

    # silent-defaults: allow-contract -- The synthetic wheel builder defaults to version 1.0, while version-identity tests may select another explicit pin.
    def make_wheel(self, name, version="1.0"):
        distribution = name.replace("-", "_")
        wheel_path = self.index / f"{distribution}-{version}-py3-none-any.whl"

        with zipfile.ZipFile(wheel_path, "w") as archive:
            prefix = f"{distribution}-{version}.dist-info"

            archive.writestr(
                f"{prefix}/METADATA",
                (
                    f"Metadata-Version: 2.1\n"
                    f"Name: {name}\n"
                    f"Version: {version}\n"
                ),
            )

            archive.writestr(
                f"{prefix}/WHEEL",
                (
                    "Wheel-Version: 1.0\n"
                    "Root-Is-Purelib: true\n"
                    "Tag: py3-none-any\n"
                ),
            )

            archive.writestr(f"{prefix}/RECORD", "")

        return wheel_path

    def test_pinned_bootstrap_wheel_downloads_once_and_reuses(self):
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
            pin = {
                "name": "synthetic-tool.pyz",
                "sha256": hashlib.sha256(content).hexdigest(),
                "url": f"http://127.0.0.1:{server.server_port}/synthetic-tool.pyz"
            }

            with patch.object(provider_core_acquire, "require_download_proxy"), patch.object(provider_core_acquire, "https_download_opener", return_value=urllib.request.build_opener()):
                acquired_tool_path = provider_core_acquire.acquire_artifact(self.pool, pin)

                provider_core_acquire.acquire_artifact(self.pool, pin)

            self.assertEqual(content, acquired_tool_path.read_bytes())
            self.assertEqual(["/synthetic-tool.pyz"], requests)
            self.assertFalse(list(self.pool.glob("*.part")))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_cached_artifact_is_reusable_during_shared_pool_access(self):
        content = b"synthetic tool release"
        pin = {"name": "synthetic-tool.pyz", "sha256": hashlib.sha256(content).hexdigest()}

        (self.pool / pin["name"]).write_bytes(content)
        finished = Event()
        errors = []

        def acquire():
            try:
                provider_core_acquire.acquire_artifact(self.pool, pin)
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

        def write_report(wheel_path):
            report.write_text(
                json.dumps(
                    {
                        "version": "1",
                        "install": [
                            {
                                "download_info": {
                                    "url": wheel_path.as_uri(),
                                    "archive_info": {"hashes": {"sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest()}}
                                }
                            }
                        ]
                    }
                )
            )

        write_report(self.pool / self.cached.name)

        with self.assertRaisesRegex(ValueError, r"selected_artifact=.* should match required_release_artifact="):
            wheelhouse.wheel_inputs_from_report(self.lock, self.pool, report, (candidate_dir,), {candidate.name: candidate})

        write_report(candidate)
        selected = wheelhouse.wheel_inputs_from_report(self.lock, self.pool, report, (candidate_dir,), {candidate.name: candidate})

        self.assertIn(candidate.name, selected["files"])
