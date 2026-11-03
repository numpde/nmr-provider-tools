"""Pip selects locked wheels from the shared pool."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

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

    def make_wheel(self, name):
        distribution = name.replace("-", "_")
        path = self.index / f"{distribution}-1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            prefix = f"{distribution}-1.0.dist-info"
            archive.writestr(f"{prefix}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
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
