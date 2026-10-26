"""Keep donor coverage explicit while allowing new package-owned proofs."""

from tools.container_guard import require_container

require_container()

import ast
import json
import unittest
from pathlib import Path

from tests.repository.quality.sources import ROOT


class ProviderCorePortTests(unittest.TestCase):
    def test_donor_files_and_distinct_test_methods_remain_accounted_for(self):
        manifest = json.loads((ROOT / "tests/repository/provider-core-sources.json").read_text())
        self.assertEqual("d09b77b951551c977e36bc87b5822315a4d1e944", manifest["commit"])
        paths = [entry["path"] for entry in manifest["files"]]
        self.assertEqual(len(paths), len(set(paths)))
        for entry in manifest["files"]:
            path = ROOT / entry["path"]
            with self.subTest(path=entry["path"]):
                self.assertTrue(path.is_file() and not path.is_symlink())
                if path.suffix != ".py":
                    continue
                tree = ast.parse(path.read_text())
                present = {
                    f"{owner.name}.{method.name}" for owner in ast.walk(tree) if isinstance(owner, ast.ClassDef)
                    for method in owner.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and method.name.startswith("test_")
                }
                self.assertLessEqual(set(entry["test_methods"]), present)

    def test_bootstrap_and_installed_proxy_have_one_source_owner(self):
        self.assertFalse((ROOT / "tools/bound_http_proxy.py").exists())
        self.assertTrue((ROOT / "packages/provider-core/src/provider_core/bound_http_proxy.py").is_file())
