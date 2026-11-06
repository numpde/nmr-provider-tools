"""An explicit cache cleanup removes bytecode without masking quality failures."""

from tools.container_guard import require_container

require_container()

import subprocess
import tempfile
import unittest
from pathlib import Path

from .sources import ROOT


class PythonCacheCleanupTests(unittest.TestCase):
    def clean(self, root):
        script = ROOT / "tools/python_cache_clean.sh"
        return subprocess.run(
            ["bash", "-c", 'source "$1"; clean_first_party_bytecode "$2"', "bash", str(script), str(root)],
            capture_output=True, text=True, check=False,
        )

    def test_clean_only_bytecode_inside_first_party_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "unrelated.pyc").write_bytes(b"outside")
            for name in ("tools", "tests", "packages", "src", "containers"):
                source = root / name
                source.mkdir(parents=True)
                (source / "top_level.pyc").write_bytes(b"compiled")
                (source / "code.py").write_text("pass\n", encoding="utf-8")
                cache = source / "nested/__pycache__"
                cache.mkdir(parents=True)
                (cache / "code.cpython-312.pyc").write_bytes(b"compiled")
                (cache / "preserve.txt").write_text("not bytecode", encoding="utf-8")
                empty = source / "other/__pycache__"
                empty.mkdir(parents=True)
                (empty / "only.pyo").write_bytes(b"compiled")
            (root / "tools/external-link").symlink_to(outside, target_is_directory=True)

            result = self.clean(root)
            self.assertEqual(0, result.returncode, msg=result.stderr)
            for name in ("tools", "tests", "packages", "src", "containers"):
                source = root / name
                self.assertFalse((source / "top_level.pyc").exists())
                self.assertTrue((source / "code.py").exists())
                self.assertFalse((source / "nested/__pycache__/code.cpython-312.pyc").exists())
                self.assertTrue((source / "nested/__pycache__/preserve.txt").exists())
                self.assertFalse((source / "other/__pycache__").exists())
            self.assertTrue((root / "tools/external-link").is_symlink())
            self.assertEqual(b"outside", (outside / "unrelated.pyc").read_bytes())

    def test_rejects_symlinked_source_root_without_deleting_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            target = Path(directory) / "outside"
            target.mkdir()
            (target / "retained.pyc").write_bytes(b"untouched")
            (root / "tools").symlink_to(target, target_is_directory=True)

            result = self.clean(root)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("symlink", result.stderr)
            self.assertTrue((target / "retained.pyc").exists())

    def test_public_cleanup_target_does_not_require_a_docker_context(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "--dry-run", "checks/python-cache/clean"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("tools/python_cache_clean.sh", result.stdout)
        self.assertNotIn("Select a local Docker context", result.stderr)
        self.assertIn(
            "tests/repository/quality:\n\tbash tools/repository.sh tests/repository/quality",
            (ROOT / "Makefile").read_text(encoding="utf-8"),
        )
