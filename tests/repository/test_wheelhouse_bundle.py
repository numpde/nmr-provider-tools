"""The released bundle uses the declared dependency bytes and source."""

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_wheelhouse_bundle import build


class WheelhouseBundleTests(unittest.TestCase):
    def test_bundle_rebuild_is_stable_and_refuses_wrong_dependency_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "wheelhouse_tool").mkdir()
            for name in ("__init__.py", "__main__.py", "stream.py"):
                (root / "wheelhouse_tool" / name).write_text("# source\n")
            wheel = root / "example-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("example/__init__.py", "# dependency\n")
            lock = root / "tool.lock"
            lock.write_text(f"example==1.0 \\\n    --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n")
            first, second = root / "first.pyz", root / "second.pyz"
            build(root, first, root, lock)
            build(root, second, root, lock)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            wheel.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "pinned hash"):
                build(root, root / "wrong.pyz", root, lock)
            self.assertFalse((root / "wrong.pyz").exists())
